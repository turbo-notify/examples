"""Run the attendant.

    python -m support_agent

Starts the webhook receiver. Point your Turbo Notify webhook at
``https://<your-host>/webhooks/turbo-notify`` and the agent answers whatever
arrives there.
"""

from __future__ import annotations

import logging

import uvicorn

from support_agent.config import get_settings


def main() -> None:
    """Start the server."""
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not settings.turbo_notify_api_key:
        raise SystemExit(
            "TURBO_NOTIFY_API_KEY is required. Create a key in the Turbo Notify "
            "dashboard under API Keys, then put it in .env. See .env.example."
        )

    uvicorn.run(
        "support_agent.webhook:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
