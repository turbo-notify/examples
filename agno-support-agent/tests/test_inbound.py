"""Signature verification and deciding what is worth answering."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import pytest

from support_agent.inbound import (
    ANSWERABLE_EVENTS,
    InboundRejectedError,
    parse_question,
    verify_signature,
)

SECRET = "whsec_example"


def _sign(body: bytes, timestamp: str, secret: str = SECRET) -> str:
    return (
        "sha256="
        + hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    )


class TestVerifySignature:
    def test_a_genuine_delivery_passes(self) -> None:
        body = b'{"type":"message.received"}'
        timestamp = str(int(time.time()))
        verify_signature(
            body=body,
            timestamp=timestamp,
            signature=_sign(body, timestamp),
            secret=SECRET,
            max_age_seconds=300,
        )

    def test_a_tampered_body_fails(self) -> None:
        body = b'{"type":"message.received"}'
        timestamp = str(int(time.time()))
        signature = _sign(body, timestamp)

        with pytest.raises(InboundRejectedError, match="signature"):
            verify_signature(
                body=b'{"type":"message.received","preview":"and this"}',
                timestamp=timestamp,
                signature=signature,
                secret=SECRET,
                max_age_seconds=300,
            )

    def test_a_signature_from_another_secret_fails(self) -> None:
        body = b"{}"
        timestamp = str(int(time.time()))

        with pytest.raises(InboundRejectedError, match="signature"):
            verify_signature(
                body=body,
                timestamp=timestamp,
                signature=_sign(body, timestamp, secret="whsec_someone_else"),
                secret=SECRET,
                max_age_seconds=300,
            )

    def test_an_old_delivery_is_rejected_even_though_it_is_correctly_signed(self) -> None:
        """The signature never expires on its own — this bound is the only one."""
        body = b"{}"
        timestamp = str(int(time.time()) - 3600)

        with pytest.raises(InboundRejectedError, match="replay window"):
            verify_signature(
                body=body,
                timestamp=timestamp,
                signature=_sign(body, timestamp),
                secret=SECRET,
                max_age_seconds=300,
            )

    def test_the_timestamp_is_part_of_what_is_signed(self) -> None:
        """So an attacker cannot slide a captured body forward in time."""
        body = b"{}"
        original = str(int(time.time()) - 3600)
        signature = _sign(body, original)

        with pytest.raises(InboundRejectedError, match="signature"):
            verify_signature(
                body=body,
                timestamp=str(int(time.time())),  # freshened, but not re-signed
                signature=signature,
                secret=SECRET,
                max_age_seconds=300,
            )

    @pytest.mark.parametrize(
        ("timestamp", "signature", "expected"),
        [
            (None, "sha256=deadbeef", "Timestamp"),
            (str(int(time.time())), None, "Signature"),
            ("not-a-number", "sha256=deadbeef", "not a number"),
        ],
    )
    def test_malformed_headers_are_rejected(
        self, timestamp: str | None, signature: str | None, expected: str
    ) -> None:
        with pytest.raises(InboundRejectedError, match=expected):
            verify_signature(
                body=b"{}",
                timestamp=timestamp,
                signature=signature,
                secret=SECRET,
                max_age_seconds=300,
            )

    def test_an_empty_secret_refuses_rather_than_accepting_everything(self) -> None:
        """Fail closed. The caller decides whether to verify; this never skips."""
        with pytest.raises(InboundRejectedError, match="no signing secret"):
            verify_signature(
                body=b"{}",
                timestamp=str(int(time.time())),
                signature="sha256=whatever",
                secret="",
                max_age_seconds=300,
            )


def _received(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "id": "evt_" + "e" * 32,
        "type": "message.received",
        "number_alias": "main",
        "data": {
            "id": "msg_" + "a" * 32,
            "direction": "inbound",
            "preview": "How do quotas work?",
            "content_type": "text",
            "from": {"name": "Ana", "number": "5511999999999"},
            "to": {"number": "5511888888888"},
        },
    }
    event.update(overrides)
    return event


def test_the_answerable_events_are_stated_by_name() -> None:
    """Widening this set must be a deliberate edit to this test too.

    The same reasoning `ALLOWED_TOOLS` already gets, carried over: round 16
    added one entry with a green suite, so nothing would stop a later round
    adding three. `message.edited` is the concrete harm — it carries a
    `preview` and a `direction`, so an inbound edit of an already-answered
    message would earn a second billed reply.
    """
    assert ANSWERABLE_EVENTS == ("message.received", "message.replied")


class TestParseQuestion:
    def test_an_inbound_text_message_is_a_question(self) -> None:
        question = parse_question(_received(), answer_group_messages=False)

        assert question.preview == "How do quotas work?"
        assert question.message_id == "msg_" + "a" * 32
        assert question.number_alias == "main"
        assert question.from_name == "Ana"
        assert question.is_group is False

    @pytest.mark.parametrize(
        "event_type",
        [
            "message.sent",
            "message.delivered",
            "message.read",
            "message.reacted",
            "contact.created",
            "number.connected",
        ],
    )
    def test_other_event_types_are_not_questions(self, event_type: str) -> None:
        """Most deliveries are these. Treating them as questions is the loop bug."""
        with pytest.raises(InboundRejectedError, match="not a question"):
            parse_question(_received(type=event_type), answer_group_messages=False)

    def test_a_quoted_reply_is_a_question(self) -> None:
        """A message that quotes another arrives as `message.replied`.

        This attendant answers by quoting, so long-press and Reply is the
        gesture its own answer invites. Rejecting it made the attendant go
        silent mid-conversation, with a 202 and nothing above debug in the log.
        """
        question = parse_question(
            _received(type="message.replied"), answer_group_messages=False
        )
        assert question.preview == "How do quotas work?"

    def test_an_outbound_reply_is_still_never_answered(self) -> None:
        """`message.replied` fires in BOTH directions, so `direction` is now the
        only thing stopping the attendant from answering itself forever."""
        event = _received(type="message.replied")
        event["data"]["direction"] = "outbound"

        with pytest.raises(InboundRejectedError, match="not inbound"):
            parse_question(event, answer_group_messages=False)

    def test_an_outbound_message_is_never_answered(self) -> None:
        """An attendant that answers its own messages talks to itself, billably."""
        event = _received()
        event["data"]["direction"] = "outbound"

        with pytest.raises(InboundRejectedError, match="not inbound"):
            parse_question(event, answer_group_messages=False)

    @pytest.mark.parametrize("preview", [None, "", "   ", 123])
    def test_a_message_without_usable_text_is_skipped(self, preview: Any) -> None:
        event = _received()
        event["data"]["preview"] = preview

        with pytest.raises(InboundRejectedError, match="no text"):
            parse_question(event, answer_group_messages=False)

    def test_a_message_without_an_id_is_skipped(self) -> None:
        """There would be nothing to reply to."""
        event = _received()
        del event["data"]["id"]

        with pytest.raises(InboundRejectedError, match="no id"):
            parse_question(event, answer_group_messages=False)

    def test_a_missing_number_alias_is_refused_rather_than_defaulted(self) -> None:
        """Ids are scoped to a number; guessing `main` would 404 on an extra."""
        event = _received()
        del event["number_alias"]

        with pytest.raises(InboundRejectedError, match="number_alias"):
            parse_question(event, answer_group_messages=False)

    def test_an_extra_numbers_alias_is_carried_through(self) -> None:
        question = parse_question(_received(number_alias="support"), answer_group_messages=False)
        assert question.number_alias == "support"

    def test_a_group_message_is_skipped_by_default(self) -> None:
        event = _received()
        event["data"]["to"] = {"group_id": "grp_" + "b" * 32}

        with pytest.raises(InboundRejectedError, match="group messages are disabled"):
            parse_question(event, answer_group_messages=False)

    def test_a_group_message_is_answered_when_enabled(self) -> None:
        event = _received()
        event["data"]["to"] = {"group_id": "grp_" + "b" * 32}

        question = parse_question(event, answer_group_messages=True)
        assert question.is_group is True

    def test_a_body_without_a_data_object_is_skipped(self) -> None:
        with pytest.raises(InboundRejectedError, match="no data"):
            parse_question({"type": "message.received"}, answer_group_messages=False)

    @pytest.mark.parametrize(
        "payload",
        [
            # Malformed objects.
            {},
            {"type": None},
            {"type": "message.received", "data": []},
            # NOT objects at all. `request.json()` returns whatever valid JSON
            # arrived, and the `dict` annotation is not enforced at runtime, so
            # each of these used to reach `.get()` on the wrong type and let an
            # AttributeError escape as a 500. The earlier version of this test
            # only listed the three cases above, so it asserted a guarantee it
            # was not actually checking.
            [],
            "hello",
            123,
            None,
            True,
        ],
    )
    def test_the_parser_never_raises_something_other_than_inbound_rejected(
        self, payload: Any
    ) -> None:
        """Anything can arrive at a public URL; a crash there is a 500 and a retry."""
        with pytest.raises(InboundRejectedError):
            parse_question(payload, answer_group_messages=False)


class TestTheConfiguredNumberAnswers:
    """One webhook endpoint receives every number in the organization.

    The alias setting used to be documented and never read, so the attendant
    answered on every number whose events reached it, including a line the
    operator had deliberately kept for people.
    """

    def test_a_message_on_the_configured_number_is_answered(self) -> None:
        question = parse_question(
            _received(number_alias="support"),
            answer_group_messages=False,
            number_alias="support",
        )
        assert question.number_alias == "support"

    def test_a_message_on_a_different_number_is_left_alone(self) -> None:
        with pytest.raises(InboundRejectedError, match="answers on"):
            parse_question(
                _received(number_alias="main"),
                answer_group_messages=False,
                number_alias="support",
            )

    @pytest.mark.parametrize("arrived_on", ["main", "support", "vendas-sp"])
    def test_the_reply_goes_out_on_the_number_that_received_it(self, arrived_on: str) -> None:
        """Each delivery answers on ITS OWN alias, with no filter configured.

        Parametrised because one alias proves nothing: the previous version
        passed the same string as both the delivery's alias and the configured
        filter, so the assertion held whichever one the parser returned, and it
        was character-for-character the test above it. Several different
        deliveries is what distinguishes "carries the alias through" from
        "hardcodes one", which is the property that matters: ids are scoped to
        a number, so replying under the wrong alias is a 404.
        """
        question = parse_question(
            _received(number_alias=arrived_on),
            answer_group_messages=False,
            number_alias=None,
        )
        assert question.number_alias == arrived_on

    def test_no_filter_answers_on_every_number(self) -> None:
        """`None` means every number, which is what a BLANK setting produces.

        Not what omitting it produces: the default is `main`, so an unset
        variable answers only on the main number and drops every extra one. The
        previous docstring said the opposite, over the one branch no production
        path could reach until the setting became `str | None`.
        """
        question = parse_question(
            _received(number_alias="anything"),
            answer_group_messages=False,
            number_alias=None,
        )
        assert question.number_alias == "anything"


def test_the_signed_string_matches_the_documented_construction() -> None:
    """Pins the exact construction, `{timestamp}.{raw_body}`.

    Checked against `verify_signature` itself, not against this file's `_sign`
    helper. An earlier version compared `_sign(...)` to an inline re-derivation
    of the same expression and never called the module at all: it claimed to pin
    the published contract while only proving the test agreed with itself. If
    the module's construction drifts from the contract, this now fails.
    """
    body = json.dumps({"type": "message.received"}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    from_the_contract = (
        "sha256="
        + hmac.new(
            SECRET.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
    )

    verify_signature(
        body=body,
        timestamp=timestamp,
        signature=from_the_contract,
        secret=SECRET,
        max_age_seconds=300,
    )


class TestTheRealWebhookShape:
    """The key set of a live delivery, not one written from an assumption.

    This example read `data["text"]` until it was run against a real message.
    That field does not exist on a webhook: the delivery carries `preview` (up
    to 50 characters) and `content_type`, and the body is fetched separately.
    Every message was therefore rejected as "carries no text" and the attendant
    answered nobody, while the suite stayed green because the fixtures were
    written from the same wrong assumption as the code.

    So the payload below has every key a real one has, in the same shape. What
    it does not have is anybody's real identity: this repository is public, and
    a captured payload is by definition somebody's phone number, name and
    conversation. Ids, numbers and names are placeholders; every structural
    field is verbatim.
    """

    REAL_DELIVERY: dict[str, Any] = {
        "id": "evt_" + "c" * 32,
        "type": "message.received",
        "number_alias": "main",
        "data": {
            "id": "msg_" + "c" * 32,
            "from": {
                "contact_id": "ct_" + "d" * 32,
                "name": "Ana",
                "number": "5511999999999",
            },
            "to": {
                "contact_id": "ct_" + "e" * 32,
                "name": "Suporte",
                "alias": "main",
                "number": "5511888888888",
            },
            "preview": "Qual o preco do plano mais barato?",
            "content_type": "text",
            "direction": "inbound",
            "channel": "whatsapp",
            "origin": "native",
            "at": "2026-09-05T15:20:00Z",
        },
    }

    def test_the_fixture_names_nobody_real(self) -> None:
        """A public repository is the wrong place for a captured conversation.

        The first version of this fixture was pasted straight out of a live
        delivery, so it carried two real phone numbers and the names beside
        them. The shape is the useful part, and the shape survives placeholders.
        """
        data = self.REAL_DELIVERY["data"]
        for party in (data["from"], data["to"]):
            assert set(party["number"][4:]) <= {"8", "9"}, "use a placeholder number"

    def test_the_fixture_is_shaped_like_a_delivery_that_can_exist(self) -> None:
        """`origin` says who sent it, and an inbound one was never sent by us.

        The first version of this fixture carried `"origin": "tn"`, copied from
        the *sender's* own event stream. A test named for a captured payload
        that quietly contains an assumption is worse than no fixture: it is the
        exact failure this class was written to close, one field over.
        """
        assert self.REAL_DELIVERY["data"]["origin"] == "native"
        assert self.REAL_DELIVERY["data"]["direction"] == "inbound"

    def test_a_real_delivery_is_answerable(self) -> None:
        question = parse_question(self.REAL_DELIVERY, answer_group_messages=False)

        assert question.message_id == "msg_" + "c" * 32
        assert question.preview.startswith("Qual o preco")
        assert question.from_name == "Ana"

    def test_no_text_key_is_expected_anywhere(self) -> None:
        """The field the code used to read is genuinely absent from a real one."""
        assert "text" not in self.REAL_DELIVERY["data"]

    def test_a_non_text_message_is_declined_by_content_type(self) -> None:
        """`content_type` is how a real delivery says what it is."""
        event = json.loads(json.dumps(self.REAL_DELIVERY))
        event["data"]["content_type"] = "image"

        with pytest.raises(InboundRejectedError, match="not a text message"):
            parse_question(event, answer_group_messages=False)
