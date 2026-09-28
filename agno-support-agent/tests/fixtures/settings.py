"""Building a `Settings` in tests without inheriting the developer's `.env`.

`BaseSettings.__init__` genuinely accepts `_env_file` (and its siblings): it is
hand-written in `pydantic_settings`, not synthesized, and passing `_env_file=None`
is the documented way to skip the dotenv source entirely for one instance.

`Settings` itself declares no `__init__` of its own, though, and pydantic's
metaclass carries a `@dataclass_transform()` that makes a type checker
synthesize a fields-only `__init__` for any class that does not write one -
`Settings` included. `Settings(_env_file=None, ...)` and even
`super().__init__(_env_file=None, ...)` from a subclass both resolve against
that synthesized signature, which has never heard of `_env_file` and rejects
it (`reportCallIssue`).

Naming `BaseSettings` explicitly, instead of going through `Settings` or
`super()`, is what escapes it: pyright then resolves `__init__` against the
real, hand-written method on `BaseSettings`, which does declare `_env_file`.
"""

from __future__ import annotations

from typing import Any

from pydantic_settings import BaseSettings

from support_agent.config import Settings


class _SettingsIgnoringDotenv(Settings):
    """`Settings`, but never reads the `.env` file beside the project root."""

    def __init__(self, **data: Any) -> None:
        BaseSettings.__init__(self, _env_file=None, **data)


def settings_ignoring_dotenv(**overrides: Any) -> Settings:
    """A `Settings` built only from `overrides` and each field's own default.

    Every field left out of `overrides` gets its declared default, never a
    value read from whatever `.env` happens to sit beside this checkout: a
    test that does not pin a field (say, `turbo_notify_number_alias`) must see
    the same value on every machine, not whatever a developer set locally.
    """
    return _SettingsIgnoringDotenv(**overrides)


__all__ = ["settings_ignoring_dotenv"]
