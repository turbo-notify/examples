"""Reading an inbound Turbo Notify webhook delivery.

Two jobs, both boring and both the kind of thing that is wrong in production for
months if nobody writes them down:

* **verify** the delivery really came from Turbo Notify and is recent;
* **decide** whether it is a question from a real person at all, and whether
  this agent has anything to answer it FROM.

The first matters more than it looks. Turbo Notify delivers every event type
you subscribe to (deliveries, reads, reactions, group membership changes), and
an attendant that treats all of them as questions will happily reply to its own
outbound message and talk to itself. The second is a different failure: a
customer whose message this agent genuinely cannot read is still a customer,
and gets an apology, not a 202 into the void.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
from dataclasses import dataclass

#: The two event types that can carry a question from a customer.
#:
#: `message.replied` is the one that is easy to miss: a quoted message is
#: delivered under that type and never as `message.received`.
ANSWERABLE_EVENTS = ("message.received", "message.replied")

#: Every C0 control character plus DEL. Newlines and carriage returns are
#: control characters too, and are the ones that matter most here: a display
#: name or message text that carries one can make a single log line, or a
#: single line of the prompt, look like several.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")

#: Long enough for a full voice-note transcript, short enough that a message
#: designed to bloat the model's turn cannot grow without bound.
MAX_PREVIEW_LENGTH = 4000

#: A WhatsApp display name has never needed more than this in practice.
MAX_FROM_NAME_LENGTH = 200


def _sanitize_customer_text(value: str, *, max_length: int) -> str:
    """Make customer-controlled text safe to place inside a prompt or a log line.

    `from_name` and `preview` are both written by a stranger and land in the
    model's turn verbatim otherwise. Every control character (newlines and
    carriage returns included) becomes a single space, runs of whitespace
    collapse to one, and the result is capped at `max_length`. This does not
    filter what the text SAYS; nothing here decides a message is an attack.
    It only keeps the text from breaking the line it appears on or the
    delimited block it is placed in, and keeps one message from costing an
    unbounded number of tokens.
    """
    without_control = _CONTROL_CHARACTERS.sub(" ", value)
    collapsed = " ".join(without_control.split())
    return collapsed[:max_length]


class InboundRejectedError(Exception):
    """The delivery is not one this agent will act on, and why.

    Carries whether the delivery was *malformed* rather than merely uninteresting,
    because the two look identical to a caller and must not be logged alike.

    Most deliveries are receipts and status changes: skipping those is the
    normal case, and logging each one at anything above debug would bury the
    endpoint in noise. But "the body is not a JSON object" and "the message
    carries no text" are the shape of a contract break: a webhook whose fields
    do not match what this parser expects. Rejecting every message that way,
    silently, produces a 202 for every delivery and not one line of output at
    the default log level, which is why the two cases are logged differently.
    """

    def __init__(self, message: str, *, malformed: bool = False) -> None:
        super().__init__(message)
        self.malformed = malformed


def verify_signature(
    *,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    max_age_seconds: int,
) -> None:
    """Authenticate one webhook delivery, or raise.

    Turbo Notify signs ``{timestamp}.{raw_body}`` with your secret and sends
    ``X-Webhook-Signature: sha256=<hex>``.

    The signature is computed over the **raw bytes**, so it must be checked
    before anything parses or re-serialises them: JSON round-tripping changes
    key order and whitespace, and the recomputed digest then never matches.

    The timestamp bound is not optional decoration. Without it a captured
    delivery stays valid forever, since the signature does not expire on its
    own, so anyone who once observed one request could replay it
    indefinitely. The same bound also rejects a timestamp stamped in the
    future: nothing about a genuine delivery ever has one, so a value that
    far ahead is either a clock badly out of sync or a forged header, and
    either way it is not a delivery to trust blindly.
    """
    if not secret:
        raise InboundRejectedError("no signing secret configured")
    if not signature:
        raise InboundRejectedError("missing X-Webhook-Signature")
    if not timestamp:
        raise InboundRejectedError("missing X-Webhook-Timestamp")

    try:
        parsed_timestamp = float(timestamp)
    except ValueError as exc:
        raise InboundRejectedError("X-Webhook-Timestamp is not a number") from exc
    # `float("nan")` and `float("inf")` both parse without raising, so a
    # numeric check alone lets either one through: the age computed from
    # either is never a real number of seconds, and "not a number" (NaN)
    # compares false to every bound below, including its own.
    if not math.isfinite(parsed_timestamp):
        raise InboundRejectedError("X-Webhook-Timestamp is not a finite number")

    age = time.time() - parsed_timestamp
    if age > max_age_seconds:
        raise InboundRejectedError(
            f"delivery is {int(age)}s old; replay window is {max_age_seconds}s"
        )
    if age < -max_age_seconds:
        raise InboundRejectedError(
            f"delivery is timestamped {int(-age)}s in the future; "
            f"replay window is {max_age_seconds}s"
        )

    expected = (
        "sha256="
        + hmac.new(
            secret.encode(),
            f"{timestamp}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()
    )

    # Constant-time: a plain `==` leaks, through its timing, how much of the
    # digest a forger got right, which is enough to find the rest byte by byte.
    if not hmac.compare_digest(expected, signature):
        raise InboundRejectedError("signature does not match")


@dataclass(frozen=True)
class Question:
    """An inbound message worth some reply, not necessarily one this agent
    can actually answer.

    ``preview`` being ``None`` is the whole point of this shape existing. A
    customer who sends a photo, a location pin, or a voice note Turbo Notify
    could not transcribe is not a delivery to silently acknowledge and drop:
    "not answerable from what this agent was handed" is a different thing
    from "not worth answering", and the two must not collapse into one. Such
    a customer is a person who gets an apology and a nudge toward what does
    work, the same as a human attendant would give them. See
    ``responder.build_prompt``.

    ``content_type`` decides what ``preview`` holds when it is not ``None``:

    * ``"text"``: ``preview`` is the webhook's own cut of up to 50 characters.
      The agent still has to fetch the full body with ``get_message`` before
      answering. The preview is enough to decide there is a question, never
      enough to answer it.
    * ``"audio"``: ``preview`` is the full transcript, when one is available.
      Turbo Notify runs Bring-Your-Own Speech-to-Text before the webhook is
      even dispatched (see the transcription block documented at
      <https://docs.turbonotify.com/messages/webhook/>), so the whole
      transcribed text already sits on this same delivery, not a cut of it.
      Calling ``get_message`` here would fetch nothing this agent does not
      already have. ``transcription_reason`` carries why a transcript is
      missing, when the webhook explains one.
    """

    #: The ENVELOPE's event id (`evt_…`), stable across redeliveries.
    #:
    #: Carried so the caller can drop a duplicate. The replay window cannot:
    #: Turbo Notify stamps a FRESH timestamp on every delivery attempt, so a
    #: redelivery is never old enough to reject. The published contract says
    #: the same thing and tells every consumer to dedupe on this id.
    event_id: str
    message_id: str
    number_alias: str
    #: The raw wire value (`"text"`, `"image"`, `"sticker"`, and so on), never
    #: enumerated here, deliberately. A new WhatsApp content type Turbo Notify
    #: starts sending tomorrow still produces an honest apology today; a
    #: closed set of known types would need a code change first and silently
    #: drop everything else in the meantime.
    content_type: str
    #: `None` means this agent has nothing to answer FROM, not that there is
    #: nothing to answer TO. See the class docstring.
    preview: str | None
    from_name: str | None
    is_group: bool
    #: Who to show "typing…" to while the model composes an answer: the
    #: group, if this is a group message, or the sender's own number
    #: otherwise. Never a value the model chose: `send_typing_indicator`
    #: takes an arbitrary recipient, same as `send_text` does, and `webhook.py`
    #: calls it directly with THIS value rather than handing it to the model,
    #: which is the whole reason it is safe to use at all despite not being in
    #: `ALLOWED_TOOLS`. `None` when the webhook carried neither (malformed
    #: enough to be unusable for this, not enough to reject the question).
    group_id: str | None
    from_number: str | None
    #: Why a voice note has no transcript, when the webhook explains one:
    #: `"not_configured"`, `"provider_error"`, `"file_too_large"`, `"timeout"`,
    #: `"no_adapter"` or `"internal_error"`. `None` for every other content
    #: type, and also for an audio message the webhook carried no
    #: `transcription` block for at all. See ``responder.build_prompt`` for why
    #: `"not_configured"` gets a different apology than the other five.
    transcription_reason: str | None = None


def parse_question(
    event: object,
    *,
    answer_group_messages: bool,
    number_alias: str | None = None,
) -> Question:
    """Pull a question worth replying to out of a webhook body, or raise.

    Args:
        event: The decoded JSON body. Typed ``object`` rather than ``dict``
            deliberately: the annotation is not enforced at runtime, and
            ``request.json()`` returns whatever valid JSON arrived, including a
            list, a string or ``null``.
        answer_group_messages: Whether group messages are answerable at all.
        number_alias: When set, only answer messages that arrived on this
            number. ``None`` answers on every number reaching this webhook.

    Raises:
        InboundRejectedError: When this is not an inbound message from a real
            person at all: the wrong event type, an outbound echo, a group
            this agent was told to leave alone, a number it does not answer
            on, or a broken contract. That is the common case, not an error,
            since most deliveries are receipts and status changes. A message
            this agent genuinely cannot read (a photo, an untranscribed voice
            note) is not one of these cases: it still comes back as a
            `Question`, with `preview=None`, so it gets an apology instead of
            a silent acknowledgement.
    """
    # The body is whatever was posted, not necessarily an object. Without this
    # check, `curl -d '[]'` reaches `.get()` on a list and lets an
    # AttributeError escape as a 500, which in unverified mode any
    # unauthenticated caller who found the URL could trigger.
    if not isinstance(event, dict):
        raise InboundRejectedError("body is not a JSON object", malformed=True)

    # Both are inbound message events, and the second one is not optional. A
    # message that quotes another is delivered as `message.replied`, never as
    # `message.received`. This attendant answers with `reply_to_message`,
    # which quotes: so long-press and Reply, the gesture its own answer
    # invites, has to stay answerable, or a conversation goes silent the
    # moment the customer replies to it.
    if event.get("type") not in ANSWERABLE_EVENTS:
        raise InboundRejectedError(f"not a question: {event.get('type')!r}")

    data = event.get("data")
    if not isinstance(data, dict):
        raise InboundRejectedError("event has no data object", malformed=True)

    # `direction` is load-bearing, not belt-and-braces, since `message.replied`
    # joined the set above: that event fires in both directions, so this is
    # the only thing standing between the attendant and answering its own
    # replies forever, on a real number, at the customer's expense. Both event
    # types declare `direction` as a required field, so it is always present
    # on the event that needs it.
    #
    # `!= "inbound"`, not `not in (None, "inbound")`: an absent `direction` is
    # already a broken contract, and this guard must not read a missing field
    # as permission to answer.
    if data.get("direction") != "inbound":
        raise InboundRejectedError(f"not inbound: {data.get('direction')!r}", malformed=True)

    # The event does not carry the message body. It carries `preview`, a cut
    # of up to 50 characters, and `content_type`. The full text is fetched
    # with `get_message`, exactly as the webhook documentation prescribes.
    #
    # `content_type` itself is required, same reasoning as `direction`, so an
    # absent one is a broken contract, not license to treat the delivery as
    # readable or as skippable. Everything downstream of that check, though,
    # is a real customer message this agent will reply to. What differs is
    # only whether it has words to answer from: `preview` carries them when
    # it does, and is `None` when it does not, so the model can apologize and
    # offer an alternative instead of this endpoint going silent on a real
    # person.
    content_type = data.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        raise InboundRejectedError(
            f"content_type missing or invalid: {content_type!r}", malformed=True
        )

    preview: str | None
    transcription_reason: str | None = None
    if content_type == "text":
        raw_preview = data.get("preview")
        if not isinstance(raw_preview, str) or not raw_preview.strip():
            raise InboundRejectedError("message carries no text", malformed=True)
        preview = _sanitize_customer_text(raw_preview, max_length=MAX_PREVIEW_LENGTH) or None
        if preview is None:
            raise InboundRejectedError("message carries no text", malformed=True)
    elif content_type == "audio":
        # A voice note. `transcription` is a discriminated object, never a
        # bare string: `{"kind": "available", "text": ..., "language": ...}`
        # when Bring-Your-Own Speech-to-Text produced a result,
        # `{"kind": "unavailable", "reason": ...}` when it is not configured
        # for this number or the provider failed. The second shape is the
        # common case, not an error: the message itself is never blocked by a
        # transcription failure (see the transcription block documented at
        # <https://docs.turbonotify.com/messages/webhook/>), and this agent
        # answers it the same way it answers a photo it cannot read: an
        # apology, not silence. See ``responder.build_prompt`` for how the
        # apology differs by `reason`.
        transcription = data.get("transcription")
        preview = None
        if isinstance(transcription, dict):
            if transcription.get("kind") == "available":
                text = transcription.get("text")
                if isinstance(text, str) and text.strip():
                    preview = _sanitize_customer_text(text, max_length=MAX_PREVIEW_LENGTH) or None
            elif transcription.get("kind") == "unavailable":
                raw_reason = transcription.get("reason")
                transcription_reason = raw_reason if isinstance(raw_reason, str) else None
    else:
        # Media, location, a sticker, a calendar invite: anything this
        # example does not turn into words. Still a real question from a real
        # person; see the module and `Question` docstrings for why this does
        # not raise.
        preview = None

    message_id = data.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise InboundRejectedError(
            "message has no id, so there is nothing to reply to", malformed=True
        )

    # The envelope's `number_alias` says which of your numbers received this.
    # It is required rather than defaulted: ids are scoped to a number, so
    # replying under the wrong alias is a 404 rather than a wrong answer.
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise InboundRejectedError("envelope has no id", malformed=True)

    arrived_on = event.get("number_alias")
    if not isinstance(arrived_on, str) or not arrived_on:
        raise InboundRejectedError("envelope has no number_alias", malformed=True)

    # One webhook endpoint receives every number in the organization. Without
    # this check the attendant answers on all of them, including a line staff
    # handle by hand: automated replies on a number the operator meant to
    # exclude, spending that number's quota, with nothing said about it.
    if number_alias is not None and arrived_on != number_alias:
        raise InboundRejectedError(
            f"arrived on {arrived_on!r}, but this agent answers on {number_alias!r}"
        )

    to_party = data.get("to")
    is_group = isinstance(to_party, dict) and "group_id" in to_party
    if is_group and not answer_group_messages:
        raise InboundRejectedError("group messages are disabled for this agent")

    group_id = to_party.get("group_id") if is_group and isinstance(to_party, dict) else None
    if not isinstance(group_id, str) or not group_id:
        group_id = None

    from_party = data.get("from")
    raw_from_name = from_party.get("name") if isinstance(from_party, dict) else None
    from_name = (
        _sanitize_customer_text(raw_from_name, max_length=MAX_FROM_NAME_LENGTH) or None
        if isinstance(raw_from_name, str)
        else None
    )
    from_number = from_party.get("number") if isinstance(from_party, dict) else None
    if not isinstance(from_number, str) or not from_number:
        from_number = None

    return Question(
        event_id=event_id,
        message_id=message_id,
        # `arrived_on`, never the configured filter: the reply must go out on
        # the number that received the message, because message ids are scoped
        # to a number.
        number_alias=arrived_on,
        content_type=content_type,
        preview=preview,
        from_name=from_name,
        is_group=is_group,
        group_id=group_id,
        from_number=from_number,
        transcription_reason=transcription_reason,
    )


__all__ = ["InboundRejectedError", "Question", "parse_question", "verify_signature"]
