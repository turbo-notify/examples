"""The HTTP surface: one endpoint that receives Turbo Notify webhooks.

Two properties this endpoint has to hold, and they pull in opposite directions:

* **Answer fast.** Turbo Notify retries a delivery that does not get a 2xx
  promptly, and an LLM turn plus a couple of tool calls takes seconds. Doing the
  work inline means the same question gets answered twice, or three times.
* **Never lose one.** Answering 202 and then dropping the work is worse than
  being slow — the customer waits for a reply that is never coming.

So: verify and parse synchronously (fast, and the only part that can legitimately
reject), then hand the answer to a background task and acknowledge. If the
background task fails, it is logged loudly. A real deployment would put a queue
there; an example that pretended to would bury the interesting part under
infrastructure.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, Request, Response, status

from support_agent.config import Settings, get_settings
from support_agent.inbound import InboundRejectedError, parse_question, verify_signature
from support_agent.responder import Attendant, answer

#: How many event ids to remember. A delivery is retried within minutes, so a
#: few thousand covers any realistic burst; the bound is what stops an
#: always-on process from growing without limit. Per-process and in-memory on
#: purpose: this is an example, and a real deployment would put the same check
#: in whatever store its queue already uses.
SEEN_EVENT_CAPACITY = 4096

logger = logging.getLogger("support_agent.webhook")


def create_app(
    settings: Settings | None = None,
    attendant_factory: Any = None,
) -> FastAPI:
    """Build the receiver.

    Args:
        settings: Overridden in tests.
        attendant_factory: An async context manager yielding an
            :class:`Attendant`. Injected so the tests can run the whole HTTP
            path with no model, no MCP server and no API key — the alternative
            is a suite that only runs when three external things are up, which
            in practice means a suite nobody runs.
    """
    resolved = settings or get_settings()
    factory = attendant_factory or _default_attendant_factory

    # Turbo Notify retries a delivery that does not get a prompt 2xx, and the
    # envelope `id` is stable across those attempts while the timestamp is NOT:
    # a fresh one is stamped per attempt, so the replay window can never reject
    # a redelivery. Without this, every retry costs another real WhatsApp
    # message, another message-quota unit and another model turn.
    seen: OrderedDict[str, None] = OrderedDict()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with factory(resolved) as attendant:
            _app.state.attendant = attendant
            _warn_if_unverified(resolved)
            yield

    app = FastAPI(
        title="Turbo Notify Agno support attendant",
        description="Receives WhatsApp messages by webhook and answers them.",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/turbo-notify", status_code=status.HTTP_202_ACCEPTED)
    async def receive(
        request: Request,
        background: BackgroundTasks,
        x_webhook_signature: str | None = Header(default=None),
        x_webhook_timestamp: str | None = Header(default=None),
    ) -> Response:
        # The raw bytes, before anything parses them: the signature covers
        # exactly these, and a JSON round-trip changes key order and whitespace.
        body = await request.body()

        if resolved.verifies_signatures:
            try:
                verify_signature(
                    body=body,
                    timestamp=x_webhook_timestamp,
                    signature=x_webhook_signature,
                    secret=resolved.turbo_notify_webhook_secret,
                    max_age_seconds=resolved.webhook_max_age_seconds,
                )
            except InboundRejectedError as exc:
                logger.warning("Rejected a webhook delivery: %s", exc)
                # 401, not 400: this is an authentication failure, and Turbo
                # Notify should not keep retrying something it cannot sign
                # correctly.
                return Response(status_code=status.HTTP_401_UNAUTHORIZED)

        try:
            event = await request.json()
        except ValueError:
            logger.warning("Webhook delivery was not JSON")
            return Response(status_code=status.HTTP_400_BAD_REQUEST)

        try:
            question = parse_question(
                event,
                answer_group_messages=resolved.answer_group_messages,
                number_alias=resolved.turbo_notify_number_alias,
            )
        except InboundRejectedError as exc:
            # 202 either way, because the delivery itself was accepted and a 4xx
            # would only make Turbo Notify retry something retrying cannot fix.
            # The LOG level is where the two cases part.
            if exc.malformed:
                # The delivery did not look like the documented contract. Said
                # out loud, because the silent version of this is what hid the
                # `data["text"]` bug: every message rejected, every delivery
                # 202'd, nothing in the log at the default level.
                logger.warning("Delivery did not match the expected shape: %s", exc)
            else:
                # The common case, not an error: most deliveries are receipts
                # and status changes, and this agent has nothing to do with them.
                logger.debug("Nothing to answer: %s", exc)
            return Response(status_code=status.HTTP_202_ACCEPTED)

        if question.event_id in seen:
            logger.info(
                "Ignoring a redelivery of %s (already answered)", question.event_id
            )
            return Response(status_code=status.HTTP_202_ACCEPTED)
        seen[question.event_id] = None
        while len(seen) > SEEN_EVENT_CAPACITY:
            seen.popitem(last=False)

        background.add_task(_answer_safely, question, app.state.attendant)
        return Response(status_code=status.HTTP_202_ACCEPTED)

    return app


async def _answer_safely(question: Any, attendant: Attendant) -> None:
    """Answer, and never let a failure escape into the server's task group.

    An exception from a background task is not returned to anyone: the customer
    already got their 202. Logging it is the only way it is ever seen, so it is
    logged with the message id, which is what makes it findable afterwards.

    A failure here is usually *not* an exception, though. A model that returns
    404, or a model that answers in plain text instead of calling the reply
    tool, both come back as an ordinary result. So the log reports what
    :func:`answer` observed, not merely that it returned.
    """
    try:
        outcome = await answer(question, attendant)
    except Exception:
        logger.exception("Failed to answer %s on %s", question.message_id, question.number_alias)
        return

    if outcome.replied:
        logger.info("Answered %s on %s", question.message_id, question.number_alias)
    else:
        logger.error(
            "Did not answer %s on %s: %s",
            question.message_id,
            question.number_alias,
            outcome.detail,
        )


@asynccontextmanager
async def _default_attendant_factory(settings: Settings) -> AsyncIterator[Attendant]:
    """Open the MCP connection and build the agent around it.

    ``MCPTools`` is a context manager: the session is established on entry and
    torn down on exit. Building it once for the process lifetime, rather than
    per request, is what keeps a reply to about one round trip.
    """
    from support_agent.agent import build_agent, build_mcp_tools

    async with build_mcp_tools(settings) as tools:
        yield build_agent(settings, tools)


def _warn_if_unverified(settings: Settings) -> None:
    """Say so, loudly, when deliveries are not authenticated."""
    if settings.verifies_signatures:
        return
    logger.warning(
        "TURBO_NOTIFY_WEBHOOK_SECRET is not set: webhook signatures are NOT "
        "being verified. Anyone who learns this URL can make the agent answer "
        "whatever they like, on your number and at your expense. Fine behind a "
        "tunnel on your laptop; never anywhere reachable."
    )


__all__ = ["create_app"]
