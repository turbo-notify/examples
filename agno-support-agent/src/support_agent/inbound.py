"""Reading an inbound Turbo Notify webhook delivery.

Two jobs, both boring and both the kind of thing that is wrong in production for
months if nobody writes them down:

* **verify** the delivery really came from Turbo Notify and is recent;
* **decide** whether it is a question from a real person at all, and whether
  this agent has anything to answer it FROM.

The first matters more than it looks. Turbo Notify delivers every event type
you subscribe to — deliveries, reads, reactions, group membership changes — and
an attendant that treats all of them as questions will happily reply to its own
outbound message and talk to itself. The second is a different failure: a
customer whose message this agent genuinely cannot read is still a customer,
and gets an apology, not a 202 into the void.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

#: The two event types that can carry a question from a customer.
#:
#: `message.replied` is the one that is easy to miss: a quoted message is
#: delivered under that type and never as `message.received`.
ANSWERABLE_EVENTS = ("message.received", "message.replied")


class InboundRejectedError(Exception):
    """The delivery is not one this agent will act on, and why.

    Carries whether the delivery was *malformed* rather than merely uninteresting,
    because the two look identical to a caller and must not be logged alike.

    Most deliveries are receipts and status changes: skipping those is the
    normal case and logging each one at anything above debug would bury the
    endpoint in noise. But "the body is not a JSON object" and "the message
    carries no text" are the shape of a **contract break**, and this example has
    already been through one: it read a field webhooks do not have, rejected
    every message as having no text, and answered nobody. At the default log
    level that produced a 202 for every delivery and not one line of output.
    Silence is exactly what made it hard to see.
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
    delivery stays valid forever — the signature does not expire on its own —
    so anyone who once observed one request can replay it indefinitely.
    """
    if not secret:
        raise InboundRejectedError("no signing secret configured")
    if not signature:
        raise InboundRejectedError("missing X-Webhook-Signature")
    if not timestamp:
        raise InboundRejectedError("missing X-Webhook-Timestamp")

    try:
        age = time.time() - float(timestamp)
    except ValueError as exc:
        raise InboundRejectedError("X-Webhook-Timestamp is not a number") from exc
    if age > max_age_seconds:
        raise InboundRejectedError(
            f"delivery is {int(age)}s old; replay window is {max_age_seconds}s"
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
    """An inbound message worth *some* reply — not necessarily one this agent
    can actually answer.

    ``preview`` being ``None`` is the whole point of this shape existing: a
    customer who sends a photo, a location pin, or a voice note Turbo Notify
    could not transcribe is not a delivery to silently 202 away. They are a
    person who gets an apology and a nudge toward what DOES work, same as a
    human attendant would give them — see ``responder.build_prompt``. This
    example used to fold "cannot answer" into "not worth answering" and
    dropped both the same way; the two are not the same thing.

    ``content_type`` decides what ``preview`` holds when it is not ``None``:

    * ``"text"`` — ``preview`` is the webhook's own cut of up to 50 characters.
      The agent still has to fetch the full body with ``get_message`` before
      answering: the preview is enough to decide there IS a question, never
      enough to answer it.
    * ``"audio"`` — ``preview`` is the FULL transcript. Turbo Notify runs
      Bring-Your-Own Speech-to-Text before the webhook is even dispatched (ADR
      2026-06-29), so the whole transcribed text already sits on this same
      delivery, not a cut of it. Calling ``get_message`` here would fetch
      nothing this agent does not already have.
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
    #: The raw wire value (`"text"`, `"image"`, `"sticker"`, …) — never
    #: enumerated here, deliberately. A new WhatsApp content type Turbo Notify
    #: starts sending tomorrow still produces an honest apology today; a
    #: closed set of known types would need a code change first and silently
    #: drop everything else in the meantime.
    content_type: str
    #: `None` means this agent has nothing to answer FROM — not that there is
    #: nothing to answer TO. See the class docstring.
    preview: str | None
    from_name: str | None
    is_group: bool


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
            person at all — the wrong event type, an outbound echo, a group
            this agent was told to leave alone, a number it does not answer
            on, or a broken contract. That is the common case, not an error:
            most deliveries are receipts and status changes. A message this
            agent genuinely cannot READ (a photo, an untranscribed voice note)
            is NOT one of these cases: it still comes back as a `Question`,
            with `preview=None`, so it gets an apology instead of a 202 into
            the void.
    """
    # The body is whatever was posted, not necessarily an object. Without this,
    # `curl -d '[]'` reached `.get()` on a list and left an AttributeError to
    # escape as a 500, which in unverified mode any unauthenticated caller who
    # found the URL could trigger.
    if not isinstance(event, dict):
        raise InboundRejectedError("body is not a JSON object", malformed=True)

    # BOTH inbound message events, and the second one is not optional.
    #
    # A message that QUOTES another is delivered as `message.replied`, never as
    # `message.received`. This attendant answers with `reply_to_message`, which
    # quotes: so long-press and Reply, the gesture its own answer invites, is
    # exactly the one it used to drop. It went silent mid-conversation, with a
    # 202 and nothing above debug in the log.
    if event.get("type") not in ANSWERABLE_EVENTS:
        raise InboundRejectedError(f"not a question: {event.get('type')!r}")

    data = event.get("data")
    if not isinstance(data, dict):
        raise InboundRejectedError("event has no data object", malformed=True)

    # `direction` is LOAD-BEARING, not belt-and-braces, since `message.replied`
    # joined the set above: that event fires in BOTH directions, so this is the
    # only thing standing between the attendant and answering its own replies
    # forever, on a real number, at the customer's expense. `MessageRepliedData`
    # declares `direction` required with a backfill validator, so it is always
    # present on the event that needs it.
    # `!= "inbound"`, NOT `not in (None, "inbound")`. Both event types declare
    # `direction` as required, so an absent one is already a broken contract,
    # and this guard is the only thing stopping the attendant from answering
    # its own replies forever. A guard described that way must not read a
    # missing field as permission to answer.
    if data.get("direction") != "inbound":
        raise InboundRejectedError(f"not inbound: {data.get('direction')!r}", malformed=True)

    # **The event does not carry the message body.** It carries `preview`, a cut
    # of up to 50 characters, and `content_type`. The full text is fetched with
    # `get_message`, exactly as the webhook documentation prescribes.
    #
    # This example read `data["text"]` until it was run against a real delivery:
    # that field does not exist on a webhook, so every message was rejected as
    # "carries no text" and the attendant answered nobody. The tests passed
    # because their fixtures were written from the same wrong assumption as the
    # code. Fixtures now mirror a real captured payload.
    # `content_type` itself is required — same reasoning as `direction` — so an
    # absent one is a broken contract, not license to treat the delivery as
    # readable OR as skippable. Everything downstream of that check, though, is
    # a real customer message this agent WILL reply to. What differs is only
    # whether it has words to answer from: `preview` carries them when it does,
    # and is `None` when it does not, so the model can apologize and offer an
    # alternative instead of this endpoint going silent on a real person.
    content_type = data.get("content_type")
    if not isinstance(content_type, str) or not content_type:
        raise InboundRejectedError(f"content_type missing or invalid: {content_type!r}", malformed=True)

    preview: str | None
    if content_type == "text":
        raw_preview = data.get("preview")
        if not isinstance(raw_preview, str) or not raw_preview.strip():
            raise InboundRejectedError("message carries no text", malformed=True)
        preview = raw_preview.strip()
    elif content_type == "audio":
        # A voice note. `transcription` is a discriminated object, never a bare
        # string — {"kind": "available", "text": ..., "language": ...} when
        # Bring-Your-Own Speech-to-Text produced a result, {"kind":
        # "unavailable", "reason": ...} when it is not configured for this
        # organization or the provider failed. The second shape is the common
        # case, not an error: Turbo Notify's own fail-open invariant (ADR
        # 2026-06-29) already promises the message itself was never blocked by
        # a transcription failure, and this agent answers it the same way it
        # answers a photo it cannot read — an apology, not silence.
        transcription = data.get("transcription")
        preview = None
        if isinstance(transcription, dict) and transcription.get("kind") == "available":
            text = transcription.get("text")
            if isinstance(text, str) and text.strip():
                preview = text.strip()
    else:
        # Media, location, a sticker, a calendar invite — anything this
        # example does not turn into words. Still a real question from a real
        # person; see the module and `Question` docstrings for why this no
        # longer raises.
        preview = None

    message_id = data.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise InboundRejectedError("message has no id, so there is nothing to reply to", malformed=True)

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
    # answer by hand — automated replies on a number the operator meant to
    # exclude, spending that number's quota, with nothing said about it.
    if number_alias is not None and arrived_on != number_alias:
        raise InboundRejectedError(
            f"arrived on {arrived_on!r}, but this agent answers on {number_alias!r}"
        )

    to_party = data.get("to")
    is_group = isinstance(to_party, dict) and "group_id" in to_party
    if is_group and not answer_group_messages:
        raise InboundRejectedError("group messages are disabled for this agent")

    from_party = data.get("from")
    from_name = from_party.get("name") if isinstance(from_party, dict) else None

    return Question(
        event_id=event_id,
        message_id=message_id,
        # `arrived_on`, never the configured filter: the reply must go out on
        # the number that received the message, because message ids are scoped
        # to a number.
        number_alias=arrived_on,
        content_type=content_type,
        preview=preview,
        from_name=from_name if isinstance(from_name, str) else None,
        is_group=is_group,
    )


__all__ = ["InboundRejectedError", "Question", "parse_question", "verify_signature"]
