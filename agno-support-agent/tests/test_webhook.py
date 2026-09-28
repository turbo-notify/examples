"""The receiver, exercised end to end with no model and no MCP server.

The attendant is faked at the one seam that exists for it. That is what lets
this suite run on every change, on a laptop with no keys. A suite that needs
three external services up is a suite nobody runs, and then the HTTP path is
the part nobody tests.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import logging
import pathlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from asgi_lifespan import LifespanManager

from support_agent.config import Settings
from support_agent.webhook import UnsignedWebhookNotAllowedError, create_app

from .fixtures.settings import settings_ignoring_dotenv

SECRET = "whsec_example"
ENDPOINT = "/webhooks/turbo-notify"


def _reply_call(refused: str | None = None) -> ToolExecution:
    """One tool call, built from the agent library's OWN type, not a stub.

    A stub with only `tool_name`/`tool_call_error` cannot express a refusal,
    which arrives as result TEXT on a call the library records as successful.
    That gap is what let a broken `read_outcome` pass a green suite.
    """
    return ToolExecution(
        tool_name="reply_to_message",
        tool_call_error=False,
        result=(
            f"Error from MCP tool 'reply_to_message': {refused}"
            if refused
            else json.dumps({"message_id": "msg_" + "f" * 32, "status": {"state": "pending"}})
        ),
    )


def fake_run(*, silent: bool = False, refused: str | None = None) -> RunOutput:
    """One finished turn.

    The default called `reply_to_message` and it succeeded, because that call is
    what makes a reply a reply. ``silent`` is the run that answers nobody;
    ``refused`` is the server turning the reply down.
    """
    tools = [] if silent else [_reply_call(refused)]
    return RunOutput(status=RunStatus.completed, tools=tools)


class FakeAttendant:
    """Records the prompts it was asked to answer."""

    def __init__(
        self,
        *,
        fail: bool = False,
        silent: bool = False,
        refused: str | None = None,
        fail_typing: bool = False,
    ) -> None:
        self.prompts: list[str] = []
        self.typing_calls: list[Any] = []
        self.fail = fail
        self.silent = silent
        self.refused = refused
        self.fail_typing = fail_typing

    async def arun(self, input: str) -> object:  # noqa: A002 - the library's name
        if self.fail:
            raise RuntimeError("the model is down")
        self.prompts.append(input)
        return fake_run(silent=self.silent, refused=self.refused)

    async def show_typing(self, question: Any) -> None:
        if self.fail_typing:
            raise RuntimeError("the MCP server is down")
        self.typing_calls.append(question)


class _FlakyThenWorkingAttendant(FakeAttendant):
    """Fails the first turn, answers normally on every one after.

    Models a real retry: the delivery's first attempt genuinely fails (the
    model is unreachable, a tool call raises), and Turbo Notify redelivers
    the same event id. That redelivery must still reach the model, not be
    dropped as an already-answered duplicate.
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def arun(self, input: str) -> object:  # noqa: A002 - the library's name
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("the model is down, just this once")
        self.prompts.append(input)
        return fake_run()


class _GatedAttendant(FakeAttendant):
    """Blocks inside `arun` until a test releases it.

    Lets a test hold one delivery "in flight" for as long as it needs to, so
    a concurrent redelivery of the same event id can be sent while the first
    is still being answered.
    """

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate

    async def arun(self, input: str) -> object:  # noqa: A002 - the library's name
        self.prompts.append(input)
        await self.gate.wait()
        return fake_run()


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "turbo_notify_api_key": "tn_test",
        "turbo_notify_webhook_secret": SECRET,
        "answer_group_messages": False,
    }
    base.update(overrides)
    # Ignores the developer's own .env, or the suite silently inherits it for
    # every field these three do not pin: `turbo_notify_mcp_url` could resolve
    # to a local server instead of the default, and `turbo_notify_number_alias`
    # could disagree with the "main" the fixtures hardcode, so a reader who
    # followed the README and set a different alias would see a red suite with
    # no visible cause.
    return settings_ignoring_dotenv(**base)


def _factory_for(attendant: FakeAttendant) -> Any:
    """An attendant factory of the shape ``create_app`` expects."""

    @asynccontextmanager
    async def factory(_settings: Settings) -> AsyncIterator[FakeAttendant]:
        yield attendant

    return factory


def _signed(body: bytes, secret: str = SECRET) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = (
        "sha256="
        + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    )
    return {
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": signature,
        "Content-Type": "application/json",
    }


def _received_body(**data_overrides: Any) -> bytes:
    data: dict[str, Any] = {
        "id": "msg_" + "a" * 32,
        "direction": "inbound",
        "preview": "How do quotas work?",
        "content_type": "text",
        "from": {"name": "Ana", "number": "5511999999999"},
        "to": {"number": "5511888888888"},
    }
    data.update(data_overrides)
    return json.dumps(
        {"id": "evt_" + "e" * 32, "type": "message.received", "number_alias": "main", "data": data}
    ).encode()


@asynccontextmanager
async def _client(
    attendant: FakeAttendant, settings: Settings | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings or _settings(), attendant_factory=_factory_for(attendant))
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
            yield client


class TestHealth:
    async def test_it_answers(self) -> None:
        attendant = FakeAttendant()
        async with _client(attendant) as client:
            response = await client.get("/health")
        assert response.status_code == 200


class TestSignedDeliveries:
    async def test_a_genuine_question_reaches_the_attendant(self) -> None:
        attendant = FakeAttendant()
        body = _received_body()

        async with _client(attendant) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 202
        assert len(attendant.prompts) == 1
        prompt = attendant.prompts[0]
        assert "How do quotas work?" in prompt
        assert "msg_" + "a" * 32 in prompt
        assert "reply_to_message" in prompt

    async def test_typing_is_shown_to_the_sender_before_the_answer(self) -> None:
        """`show_typing` runs before `arun`: the whole point is covering the

        seconds the model spends composing, not the seconds after.
        """
        attendant = FakeAttendant()
        body = _received_body()

        async with _client(attendant) as client:
            await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert len(attendant.typing_calls) == 1
        assert attendant.typing_calls[0].from_number == "5511999999999"

    async def test_typing_targets_the_group_for_a_group_question(self) -> None:
        attendant = FakeAttendant()
        settings = _settings(answer_group_messages=True)
        body = _received_body(to={"group_id": "grp_" + "b" * 32})

        async with _client(attendant, settings) as client:
            await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert attendant.typing_calls[0].group_id == "grp_" + "b" * 32

    async def test_a_typing_failure_never_costs_the_real_answer(self) -> None:
        """Cosmetic feedback, not a gate: the customer still gets answered."""
        attendant = FakeAttendant(fail_typing=True)
        body = _received_body()

        async with _client(attendant) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 202
        assert len(attendant.prompts) == 1

    async def test_an_unsigned_delivery_is_rejected_and_never_reaches_the_attendant(
        self,
    ) -> None:
        attendant = FakeAttendant()
        body = _received_body()

        async with _client(attendant) as client:
            response = await client.post(
                ENDPOINT, content=body, headers={"Content-Type": "application/json"}
            )

        assert response.status_code == 401
        assert attendant.prompts == []

    async def test_a_forged_signature_is_rejected(self) -> None:
        attendant = FakeAttendant()
        body = _received_body()

        async with _client(attendant) as client:
            response = await client.post(
                ENDPOINT, content=body, headers=_signed(body, secret="whsec_wrong")
            )

        assert response.status_code == 401
        assert attendant.prompts == []

    async def test_a_body_swapped_after_signing_is_rejected(self) -> None:
        """Proves the signature is checked against the RAW bytes received."""
        attendant = FakeAttendant()
        headers = _signed(_received_body())

        async with _client(attendant) as client:
            response = await client.post(
                ENDPOINT, content=_received_body(text="something else"), headers=headers
            )

        assert response.status_code == 401
        assert attendant.prompts == []


class TestDeliveriesWorthIgnoring:
    async def test_a_receipt_is_acknowledged_without_being_answered(self) -> None:
        """202, not 4xx: the delivery was valid, there was just nothing to do."""
        attendant = FakeAttendant()
        body = json.dumps(
            {
                "id": "evt_" + "d" * 32,
                "type": "message.delivered",
                "number_alias": "main",
                "data": {"id": "msg_x"},
            }
        ).encode()

        async with _client(attendant) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 202
        assert attendant.prompts == []

    async def test_the_agents_own_outbound_message_is_not_answered(self) -> None:
        """The loop that would otherwise bill the customer forever."""
        attendant = FakeAttendant()
        body = _received_body(direction="outbound")

        async with _client(attendant) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 202
        assert attendant.prompts == []

    async def test_a_non_json_body_is_a_400(self) -> None:
        attendant = FakeAttendant()
        body = b"this is not json"

        async with _client(attendant) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 400
        assert attendant.prompts == []

    @pytest.mark.parametrize("body", [b"[]", b'"hello"', b"123", b"null", b"true"])
    async def test_valid_json_that_is_not_an_object_is_acknowledged_not_a_500(
        self, body: bytes
    ) -> None:
        """Each of these is valid JSON, so it gets past the decode guard.

        They then reached `.get()` on a list, a string, an int and `None`, and
        the AttributeError escaped as a 500. In unverified mode that made the
        endpoint crashable by anyone who found the URL, with no signature.
        """
        attendant = FakeAttendant()

        async with _client(attendant) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 202
        assert attendant.prompts == []


class TestUnverifiedMode:
    async def test_without_a_secret_deliveries_are_accepted_when_explicitly_allowed(
        self,
    ) -> None:
        """Documented as a local-tunnel convenience, and warned about at startup."""
        attendant = FakeAttendant()
        settings = _settings(turbo_notify_webhook_secret="", turbo_notify_allow_unsigned=True)
        body = _received_body()

        async with _client(attendant, settings) as client:
            response = await client.post(
                ENDPOINT, content=body, headers={"Content-Type": "application/json"}
            )

        assert response.status_code == 202
        assert len(attendant.prompts) == 1

    async def test_the_startup_warning_names_the_variable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        attendant = FakeAttendant()
        settings = _settings(turbo_notify_webhook_secret="", turbo_notify_allow_unsigned=True)

        with caplog.at_level("WARNING"):
            async with _client(attendant, settings):
                pass

        assert "TURBO_NOTIFY_WEBHOOK_SECRET" in caplog.text


class TestUnsignedBootRefusal:
    """Starting unsigned has to be a choice, not a default nobody noticed."""

    def test_refuses_to_start_without_a_secret_or_an_explicit_opt_in(self) -> None:
        with pytest.raises(UnsignedWebhookNotAllowedError):
            create_app(_settings(turbo_notify_webhook_secret=""))

    def test_starts_when_the_opt_in_is_explicit(self) -> None:
        app = create_app(
            _settings(turbo_notify_webhook_secret="", turbo_notify_allow_unsigned=True),
            attendant_factory=_factory_for(FakeAttendant()),
        )
        assert app is not None

    def test_starts_normally_with_a_secret_configured(self) -> None:
        """The opt-in is never required when the agent is signed as documented."""
        app = create_app(_settings(), attendant_factory=_factory_for(FakeAttendant()))
        assert app is not None


class TestFailureIsolation:
    async def test_a_failing_attendant_does_not_fail_the_delivery(self) -> None:
        """The customer already has their 202; a raise here would only log a 500."""
        attendant = FakeAttendant(fail=True)
        body = _received_body()

        async with _client(attendant) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 202

    async def test_the_failure_is_logged_with_the_message_id(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A background failure reaches nobody unless it is logged findably."""
        attendant = FakeAttendant(fail=True)
        body = _received_body()

        with caplog.at_level("ERROR"):
            async with _client(attendant) as client:
                await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert "msg_" + "a" * 32 in caplog.text

    async def test_a_turn_that_answered_nobody_is_not_logged_as_answered(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The failure mode that produced a clean log through a live outage.

        The model was pointed at a retired id, every call came back 404, and the
        agent library returned each run rather than raising. The endpoint logged
        `Answered msg_… on main` every time, so the only place the outage was
        visible was the customer's silent phone.
        """
        attendant = FakeAttendant(silent=True)
        body = _received_body()

        with caplog.at_level("INFO"):
            async with _client(attendant) as client:
                await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert "Answered" not in caplog.text
        assert "Did not answer" in caplog.text
        assert "msg_" + "a" * 32 in caplog.text

    async def test_a_long_tool_result_is_truncated_before_it_reaches_the_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The failure detail can carry MCP-server error text, unbounded.

        A log line is not the place to reproduce an arbitrarily long payload,
        whatever produced it: this caps it rather than growing the log file
        without bound on every refusal.
        """
        attendant = FakeAttendant(refused="x" * 5000)
        body = _received_body()

        with caplog.at_level("ERROR"):
            async with _client(attendant) as client:
                await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert "truncated" in caplog.text
        assert "x" * 5000 not in caplog.text

    async def test_a_delivery_that_breaks_the_contract_is_said_out_loud(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A malformed delivery must be visible, never silent.

        It is still answered 202, because retrying cannot fix it, so the log
        is the only place it is visible. Logging every rejection at `debug`
        would make a contract change produce a 202 for every delivery, an
        attendant that answers nobody, and not one line at the default level.
        """
        body = _received_body()
        payload = json.loads(body)
        del payload["data"]["preview"]
        broken = json.dumps(payload).encode()

        with caplog.at_level(logging.DEBUG):
            async with _client(FakeAttendant()) as client:
                response = await client.post(ENDPOINT, content=broken, headers=_signed(broken))

        assert response.status_code == 202
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "a malformed delivery was rejected silently"
        assert "did not match the expected shape" in caplog.text

    async def test_a_blank_alias_answers_on_every_number(self) -> None:
        """The most natural way to turn the filter off must not mute the agent.

        Typed `str`, `TURBO_NOTIFY_NUMBER_ALIAS=` produced `""`, which matched
        no delivery's alias, so every message was dropped: 202 to everything, an
        attendant answering nobody, nothing in the log at the default level.
        """
        attendant = FakeAttendant()
        body = _received_body()
        settings = _settings(turbo_notify_number_alias="")

        async with _client(attendant, settings) as client:
            response = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert response.status_code == 202
        assert attendant.prompts, "a blank alias filtered out every number"

    async def test_the_default_answers_only_on_the_main_number(self) -> None:
        """The other half: unset is `main`, not `every`."""
        attendant = FakeAttendant()
        payload = json.loads(_received_body())
        payload["number_alias"] = "vendas"
        body = json.dumps(payload).encode()

        async with _client(attendant) as client:
            await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert attendant.prompts == [], "a message on another number was answered"

    async def test_a_redelivery_is_answered_once(self) -> None:
        """A retry carries the SAME envelope id and a FRESH timestamp.

        The freshness is the point: `verify_signature`'s replay window can
        never reject a redelivery, because Turbo Notify stamps a new timestamp
        on every attempt. Without deduping on the envelope id, each retry
        would cost another real WhatsApp message, another message-quota unit
        and another model turn. Signed twice here, exactly as a real retry
        arrives.
        """
        attendant = FakeAttendant()
        body = _received_body()

        async with _client(attendant) as client:
            first = await client.post(ENDPOINT, content=body, headers=_signed(body))
            second = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert first.status_code == 202
        assert second.status_code == 202
        assert (
            len(attendant.prompts) == 1
        ), "the redelivery was answered a second time, at the customer's expense"

    async def test_a_retry_after_a_failed_answer_is_answered(self) -> None:
        """A failed attempt must release the event id, not lock it forever.

        The first delivery fails genuinely (the model was down for that one
        turn). Turbo Notify redelivers the same event id, and this second
        attempt must reach the model: marking an id "answered" only on
        success, and releasing it on failure, is what makes that possible.
        """
        attendant = _FlakyThenWorkingAttendant()
        body = _received_body()

        async with _client(attendant) as client:
            first = await client.post(ENDPOINT, content=body, headers=_signed(body))
            second = await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert first.status_code == 202
        assert second.status_code == 202
        assert attendant.calls == 2, "the retry never reached the model"
        assert len(attendant.prompts) == 1, "only the successful attempt should have answered"

    async def test_a_concurrent_duplicate_is_not_answered_twice(self) -> None:
        """Two deliveries of the SAME event id, in flight at the same time.

        Unlike the sequential redelivery above, here the second delivery
        arrives while the first is still being answered, not after it
        finished. Marking the id "in_flight" before the model is ever called,
        with no `await` between the membership check and that write, is what
        keeps the second one from reaching the model too.
        """
        gate = asyncio.Event()
        attendant = _GatedAttendant(gate)
        body = _received_body()

        async def _release_shortly() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        async with _client(attendant) as client:
            first, second, _ = await asyncio.gather(
                client.post(ENDPOINT, content=body, headers=_signed(body)),
                client.post(ENDPOINT, content=body, headers=_signed(body)),
                _release_shortly(),
            )

        assert first.status_code == 202
        assert second.status_code == 202
        assert len(attendant.prompts) == 1, "a concurrent duplicate reached the model too"

    async def test_a_different_delivery_is_still_answered(self) -> None:
        """The other direction, so the guard above cannot pass by answering nobody."""
        attendant = FakeAttendant()
        first_body = _received_body()
        second = json.loads(_received_body())
        second["id"] = "evt_" + "f" * 32
        second_body = json.dumps(second).encode()

        async with _client(attendant) as client:
            await client.post(ENDPOINT, content=first_body, headers=_signed(first_body))
            await client.post(ENDPOINT, content=second_body, headers=_signed(second_body))

        assert len(attendant.prompts) == 2

    def test_the_endpoint_answers_in_the_background(self) -> None:
        """Fast-ack is the endpoint's stated reason for existing, and was untested.

        Asserted structurally, and the reason is worth writing down: the ASGI
        transport these tests run on drains background tasks BEFORE returning
        the response, so a behavioural test cannot tell backgrounding from
        blocking. Under a real server the difference is the whole design: an
        inline answer holds the request open for a model turn, Turbo Notify
        stops waiting and retries, and the same question is answered twice.
        Same technique the sibling guards in this suite already use.
        """
        source = (
            pathlib.Path(__file__).resolve().parents[1] / "src" / "support_agent" / "webhook.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)

        backgrounded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_task"
        ]
        assert backgrounded, (
            "the answer is no longer handed to a background task, so the 202 "
            "waits for a model turn and Turbo Notify will retry the delivery"
        )

        # Scoped to the ROUTE HANDLER. `answer` is legitimately awaited inside
        # `_answer_safely`, which is what the background task runs; the property
        # is that the handler itself does not wait for it.
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "receive"
        )
        awaited_inline = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "answer"
        ]
        assert (
            not awaited_inline
        ), "the receiver awaits `answer` itself, so the 202 waits for a model turn"

    async def test_a_routine_skip_stays_quiet(self, caplog: pytest.LogCaptureFixture) -> None:
        """The other half. Most deliveries are receipts, and logging each one
        above debug would bury the endpoint in noise, which is how the warning
        above stops being read."""
        body = json.dumps(
            {
                "id": "evt_" + "d" * 32,
                "type": "message.delivered",
                "number_alias": "main",
                "data": {"id": "msg_x"},
            }
        ).encode()

        with caplog.at_level(logging.DEBUG):
            async with _client(FakeAttendant()) as client:
                await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert not [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ], "a routine receipt was logged as a problem"

    async def test_a_real_reply_is_logged_as_answered(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The other direction, so the check above cannot pass by logging nothing."""
        attendant = FakeAttendant()
        body = _received_body()

        with caplog.at_level("INFO"):
            async with _client(attendant) as client:
                await client.post(ENDPOINT, content=body, headers=_signed(body))

        assert "Answered msg_" + "a" * 32 in caplog.text
