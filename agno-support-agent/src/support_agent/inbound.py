"""Reading an inbound Turbo Notify webhook delivery.

Two jobs, both boring and both the kind of thing that is wrong in production for
months if nobody writes them down:

* **verify** the delivery really came from Turbo Notify and is recent;
* **decide** whether it is something this agent should answer at all.

The second matters more than it looks. Turbo Notify delivers every event type
you subscribe to — deliveries, reads, reactions, group membership changes — and
an attendant that treats all of them as questions will happily reply to its own
outbound message and talk to itself.
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
    """An inbound message this agent should answer.

    ``preview`` is a cut of up to 50 characters, which is all a webhook carries.
    The agent fetches the full body with ``get_message`` before answering: the
    preview is enough to decide there IS a question, never enough to answer it.
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
    preview: str
    from_name: str | None
    is_group: bool


def parse_question(
    event: object,
    *,
    answer_group_messages: bool,
    number_alias: str | None = None,
) -> Question:
    """Pull an answerable question out of a webhook body, or raise.

    Args:
        event: The decoded JSON body. Typed ``object`` rather than ``dict``
            deliberately: the annotation is not enforced at runtime, and
            ``request.json()`` returns whatever valid JSON arrived, including a
            list, a string or ``null``.
        answer_group_messages: Whether group messages are answerable at all.
        number_alias: When set, only answer messages that arrived on this
            number. ``None`` answers on every number reaching this webhook.

    Raises:
        InboundRejectedError: When the event is not an inbound text message this
            agent should answer. That is the common case, not an error: most
            deliveries are receipts and status changes.
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
    # Same reasoning as `direction`: `content_type` is required, so absent
    # means the delivery is malformed, not that it is text.
    if data.get("content_type") != "text":
        # Media, location, a sticker. Answerable in principle, but this example
        # keeps to text so the interesting part stays visible.
        raise InboundRejectedError(f"not a text message: {data.get('content_type')!r}")

    preview = data.get("preview")
    if not isinstance(preview, str) or not preview.strip():
        raise InboundRejectedError("message carries no text", malformed=True)

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
        preview=preview.strip(),
        from_name=from_name if isinstance(from_name, str) else None,
        is_group=is_group,
    )


__all__ = ["InboundRejectedError", "Question", "parse_question", "verify_signature"]
