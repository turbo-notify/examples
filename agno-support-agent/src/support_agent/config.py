"""Configuration, read once from the environment."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ModelProvider(str, Enum):
    """Which LLM backend answers the questions."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    #: Any OpenAI-compatible endpoint: OpenRouter, Groq, a local server.
    OPENAI_COMPATIBLE = "openai_compatible"


class Settings(BaseSettings):
    """Everything this example needs to run.

    ``.env`` is resolved against the project root rather than the working
    directory, so `python -m support_agent` behaves the same from anywhere.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Turbo Notify ────────────────────────────────────────────────────────
    #: The MCP server. Turbo Notify hosts it; there is nothing to run yourself.
    turbo_notify_mcp_url: str = "https://mcp.turbonotify.com/mcp"

    #: Your Turbo Notify API key. Created in the dashboard under API Keys.
    turbo_notify_api_key: str = ""

    #: Which of your numbers answers: `main`, or an extra number's alias.
    #:
    #: One webhook endpoint receives events for every number in the
    #: organization, so this is a real filter: a message that arrived on any
    #: other number is left alone, for whoever handles that line.
    #:
    #: **Blank means every number**, and the coercion below is what makes that
    #: true. Typed `str`, an empty `TURBO_NOTIFY_NUMBER_ALIAS=` was compared
    #: against each delivery's alias and matched none of them, so the attendant
    #: answered nobody, returned 202 to everything, and said nothing at the
    #: default log level. That is byte-for-byte the outage `malformed` exists to
    #: make impossible, reached by the most natural way to turn a filter off.
    turbo_notify_number_alias: str | None = "main"

    @field_validator("turbo_notify_number_alias", mode="after")
    @classmethod
    def _blank_alias_means_every_number(cls, value: str | None) -> str | None:
        return value or None

    #: The signing secret you set beside the webhook URL in the dashboard.
    #:
    #: Empty disables verification, which is acceptable while you are pointing
    #: the webhook at a tunnel on your laptop and nothing else can reach it. It
    #: is not acceptable anywhere a stranger can POST: without it, anyone who
    #: learns your URL can make the agent answer whatever they like, on your
    #: number and at your expense.
    turbo_notify_webhook_secret: str = ""

    #: Reject deliveries whose timestamp is older than this. Turbo Notify signs
    #: the timestamp along with the body, so a replayed delivery keeps a valid
    #: signature forever without this bound.
    webhook_max_age_seconds: int = Field(default=300, ge=1)

    # ─── Model ───────────────────────────────────────────────────────────────
    model_provider: ModelProvider = ModelProvider.ANTHROPIC
    model_id: str = "claude-sonnet-5"

    #: Only for `openai_compatible`: the base URL of the endpoint.
    model_base_url: str = ""

    #: The provider's key. Read from the provider's own conventional variable
    #: when this is unset, so `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` also work.
    model_api_key: str = ""

    # ─── Behaviour ───────────────────────────────────────────────────────────
    #: Answer group messages too. Off by default: an attendant that replies to
    #: every message in a busy group is a nuisance, and group sending is a
    #: paid feature on Turbo Notify.
    answer_group_messages: bool = False

    # ─── Server ──────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: str = "INFO"

    @property
    def verifies_signatures(self) -> bool:
        """Whether inbound deliveries are authenticated."""
        return bool(self.turbo_notify_webhook_secret)


@lru_cache
def get_settings() -> Settings:
    """The cached settings instance."""
    return Settings()


__all__ = ["PROJECT_ROOT", "ModelProvider", "Settings", "get_settings"]
