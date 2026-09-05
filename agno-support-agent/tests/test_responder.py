"""Composing the turn the model sees."""

from __future__ import annotations

import ast
import json
import pathlib
import re
from typing import Any

from agno.models.response import ToolExecution
from agno.run.agent import RunOutput, RunStatus

from support_agent.agent import ALLOWED_TOOLS
from support_agent.inbound import Question
from support_agent.responder import (
    MCP_FAILURE_PHRASES,
    REPLY_TOOL,
    answer,
    build_prompt,
    read_outcome,
)


def _question(**overrides: Any) -> Question:
    base: dict[str, Any] = {
        "event_id": "evt_" + "a" * 32,
        "message_id": "msg_" + "a" * 32,
        "number_alias": "main",
        "content_type": "text",
        "preview": "How do quotas work?",
        "from_name": "Ana",
        "is_group": False,
        "group_id": None,
        "from_number": "5511999999999",
    }
    base.update(overrides)
    return Question(**base)


class TestBuildPrompt:
    def test_it_carries_the_question(self) -> None:
        assert "How do quotas work?" in build_prompt(_question())

    def test_it_carries_the_ids_the_model_cannot_otherwise_know(self) -> None:
        """They arrived on the envelope, not in the conversation."""
        prompt = build_prompt(_question(number_alias="support"))
        assert "msg_" + "a" * 32 in prompt
        assert "support" in prompt

    def test_it_asks_for_exactly_one_reply(self) -> None:
        """Without this the model happily sends a follow-up the customer pays for."""
        prompt = build_prompt(_question())
        assert "exactly once" in prompt or "Reply exactly once" in prompt

    def test_it_names_the_person_when_known(self) -> None:
        assert "Ana" in build_prompt(_question())

    def test_it_omits_the_name_when_unknown(self) -> None:
        prompt = build_prompt(_question(from_name=None))
        assert "Their name is" not in prompt

    def test_a_group_question_asks_for_brevity(self) -> None:
        prompt = build_prompt(_question(is_group=True))
        assert "group chat" in prompt

    def test_a_transcribed_voice_note_carries_the_whole_transcript(self) -> None:
        prompt = build_prompt(
            _question(content_type="audio", preview="Pode confirmar o pedido de ontem?")
        )
        assert "Pode confirmar o pedido de ontem?" in prompt

    def test_a_transcribed_voice_note_is_not_told_to_fetch_the_message_again(self) -> None:
        """The whole point of skipping `get_message` for audio: Turbo Notify
        already handed this agent the FULL transcript on the webhook itself
        (ADR 2026-06-29), so instructing the model to fetch it a second time
        would cost a tool call for nothing new.
        """
        prompt = build_prompt(_question(content_type="audio"))
        assert "get_message" not in prompt

    def test_an_unreadable_message_asks_for_an_apology_and_an_alternative(self) -> None:
        """The behaviour this whole shape exists for: a customer whose message

        this agent cannot read still gets a reply, not silence.
        """
        prompt = build_prompt(_question(content_type="image", preview=None))
        assert "apolog" in prompt.lower()
        assert "image" in prompt

    def test_an_unreadable_message_is_not_told_to_fetch_anything(self) -> None:
        """There is nothing to fetch: `get_message` would return the same

        content type this agent already could not read.
        """
        prompt = build_prompt(_question(content_type="image", preview=None))
        assert "get_message" not in prompt

    def test_an_unreadable_message_still_carries_the_ids_to_reply_with(self) -> None:
        prompt = build_prompt(
            _question(content_type="sticker", preview=None, number_alias="support")
        )
        assert "msg_" + "a" * 32 in prompt
        assert "support" in prompt
        assert "Reply exactly once" in prompt

    def test_an_untranscribed_voice_note_apology_is_specific_not_generic(self) -> None:
        """This agent knows exactly what happened here — a voice note it could

        not transcribe, not some type it has never heard of — and the apology
        should say so rather than reusing the generic "message of type X"
        phrasing that fits every OTHER unreadable content type.
        """
        prompt = build_prompt(_question(content_type="audio", preview=None))
        assert "transcribe" in prompt.lower()
        assert '"audio"' not in prompt

    def test_the_prompt_names_no_tool_the_agent_was_not_given(self) -> None:
        """The allowlist and the prompt have to agree, or the model is sent hunting.

        An earlier version told the model to call `send_typing_indicator` on
        every question, by default, after that tool had been dropped from
        `ALLOWED_TOOLS`. Nothing caught it: the settings helper here hardcoded
        the flag off, so the suite never built the prompt the default produces.
        """
        prompt = build_prompt(_question())
        named = set(re.findall(r"`([a-z_]+)`", prompt))
        assert named <= set(ALLOWED_TOOLS), f"not available to the agent: {named - set(ALLOWED_TOOLS)}"
        # The other direction, and it is the one that was missing: a SUBSET
        # assertion gets easier to satisfy as mentions are deleted, so removing
        # the "First call `get_message`" instruction passed. That instruction is
        # the whole reason `get_message` is in the allowlist, and without it the
        # attendant answers a 50-character preview as if it were the question.
        assert {"get_message", REPLY_TOOL} <= named, (
            f"the prompt no longer instructs both tools it is given: {named}"
        )


def _call(tool_name: str, *, refused: str | None = None, raised: bool = False) -> ToolExecution:
    """One tool call, built from the agent library's OWN type.

    Hand-rolled stubs are what let the first version of `read_outcome` ship
    broken: they exposed `tool_name` and `tool_call_error` and nothing else, so
    they could not express the failure that actually happens. A refusal from the
    MCP server sets no error flag at all. It arrives as ordinary result TEXT on a
    call the library records as successful, and the stub had no `result` field
    for that text to live in.

    Args:
        refused: the server refused the call (an error payload, flag stays False).
        raised: the call itself raised (the rarer case, flag set).
    """
    return ToolExecution(
        tool_name=tool_name,
        tool_call_error=raised,
        # A delivered reply is the tool's own JSON result. Captured from the
        # live server: `{"message_id": …, "request_id": …, "status": {…}}`.
        # Using prose here would make the success case indistinguishable from
        # the failure cases, which is how the detection was wrong twice.
        result=(
            f"Error from MCP tool '{tool_name}': {refused}"
            if refused
            else json.dumps({"message_id": "msg_" + "f" * 32, "status": {"state": "pending"}})
        ),
    )


def _failed_call(text: str) -> ToolExecution:
    """A `reply_to_message` call whose result is one of the library's failure texts."""
    return ToolExecution(tool_name=REPLY_TOOL, tool_call_error=False, result=text)


def _run(
    status: RunStatus = RunStatus.completed, tools: list[ToolExecution] | None = None
) -> RunOutput:
    """One finished turn, built from the agent library's OWN type."""
    return RunOutput(status=status, tools=tools if tools is not None else [_call(REPLY_TOOL)])


class _Recorder:
    def __init__(self, run: object | None = None) -> None:
        self.prompts: list[str] = []
        self._run = run if run is not None else _run()

    async def arun(self, input: str) -> object:  # noqa: A002 - the library's name
        self.prompts.append(input)
        return self._run


class TestAnswer:
    async def test_it_hands_the_prompt_to_the_agent(self) -> None:
        recorder = _Recorder()
        result = await answer(_question(), recorder)

        assert len(recorder.prompts) == 1
        assert result.prompt == recorder.prompts[0]
        assert result.message_id == "msg_" + "a" * 32
        assert result.number_alias == "main"
        assert result.replied is True
        assert result.detail is None

    async def test_a_failed_run_is_not_reported_as_an_answer(self) -> None:
        """This was observed live, and the log claimed success throughout.

        The model was pointed at an id the provider had retired. Every request
        came back 404, Agno returned the run with an error status instead of
        raising, and the endpoint logged `Answered msg_… on main` for each one.
        """
        result = await answer(_question(), _Recorder(_run(status=RunStatus.error, tools=[])))

        assert result.replied is False
        assert result.detail is not None and "error" in result.detail

    async def test_a_run_that_never_called_the_reply_tool_is_not_an_answer(self) -> None:
        """Answering in plain text reaches nobody: the reply IS the tool call."""
        result = await answer(_question(), _Recorder(_run(tools=[_call("get_message")])))

        assert result.replied is False
        assert result.detail is not None and REPLY_TOOL in result.detail

    async def test_a_refusal_from_the_server_is_not_an_answer(self) -> None:
        """The failure this whole check exists for, and the one it first missed.

        A 402 quota refusal is not an exception and does not set the call's
        error flag. The MCP server answers the JSON-RPC call successfully with
        an error payload, the library turns it into result text, and the call is
        recorded as a SUCCESS. Reading the flag alone reported every refused
        reply as an answered customer, which is the outage-with-a-clean-log this
        function was written to prevent, one field over.
        """
        result = await answer(
            _question(),
            _Recorder(_run(tools=[_call(REPLY_TOOL, refused="payment_required: quota_exceeded")])),
        )

        assert result.replied is False
        assert result.detail is not None and "quota_exceeded" in result.detail

    async def test_a_call_that_raised_is_not_an_answer(self) -> None:
        """The rarer half: a genuine exception does set the flag."""
        result = await answer(_question(), _Recorder(_run(tools=[_call(REPLY_TOOL, raised=True)])))

        assert result.replied is False

    async def test_a_retry_after_a_failed_reply_counts_as_answered(self) -> None:
        """A failed tool call goes back to the model, which tries again.

        Both attempts land in the same turn, in order. Reading only the first
        would report a customer who WAS answered as unanswered, which is the
        same defect as the one this check exists to fix, pointing the other way.
        """
        result = await answer(
            _question(),
            _Recorder(
                _run(tools=[_call(REPLY_TOOL, refused="upstream timeout"), _call(REPLY_TOOL)])
            ),
        )

        assert result.replied is True
        assert result.detail is None

    async def test_every_attempt_failing_is_still_a_failure(self) -> None:
        """The other direction, so the fix above cannot become "always true"."""
        result = await answer(
            _question(),
            _Recorder(
                _run(tools=[_call(REPLY_TOOL, refused="first"), _call(REPLY_TOOL, refused="second")])
            ),
        )

        assert result.replied is False
        assert result.detail is not None and "second" in result.detail

    def test_every_known_failure_phrase_is_still_a_failure(self) -> None:
        """The three ways the library reports a call that did not deliver.

        Detection does not match these (it identifies a SUCCESS positively, so a
        fourth failure phrase needs no code change), but they are pinned for two
        reasons: they document why the positive test is the right shape, and
        each one is asserted to be read as a failure, so a change in the library
        that made one of them look like a result would fail here.

        An earlier version matched only the first of the three and reported the
        other two, including the timeout path, as answered customers.
        """
        import inspect

        from agno.utils import mcp as agno_mcp

        source = inspect.getsource(agno_mcp)
        for phrase in MCP_FAILURE_PHRASES:
            assert phrase in source, (
                f"agno no longer emits {phrase!r}. The failure vocabulary moved; "
                "re-check that a failure still cannot look like a delivered reply."
            )
            replied, detail = read_outcome(_run(tools=[_failed_call(f"{phrase} something")]))
            assert replied is False, f"{phrase!r} was read as a delivered reply"
            assert detail is not None

    def test_an_unrecognisable_result_is_not_claimed_as_an_answer(self) -> None:
        """Fail closed: a library that changes shape must not read as success."""
        replied, detail = read_outcome(object())

        assert replied is False
        assert detail is not None

    async def test_it_does_not_send_anything_itself(self) -> None:
        """The reply travels through an MCP tool the model chooses, not through here.

        This is the property the example exists to show, and it is checked on the
        MODULE, not on the fake: an assertion like ``not hasattr(recorder,
        "sent")`` reads as if it proves something and cannot fail, because
        nothing in this file could ever set that attribute. The real property is
        that ``responder.py`` has no way to send — so that is what is asserted.

        If someone later imports an HTTP client or the MCP toolkit here and sends
        the reply directly, the reply stops travelling the same path as every
        other action the model takes, and this fails.
        """
        recorder = _Recorder()
        await answer(_question(), recorder)
        assert recorder.prompts, "the agent was never given the turn"

        source = (
            pathlib.Path(__file__).resolve().parents[1] / "src" / "support_agent" / "responder.py"
        ).read_text(encoding="utf-8")
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.ImportFrom)
        }
        can_send = imported & {"httpx", "requests", "urllib", "agno", "mcp", "fastmcp"}
        assert not can_send, (
            f"responder.py imports {sorted(can_send)}, so it can now send on its own. "
            "The reply must go through a tool the model chose."
        )
