"""`.env.example` and `Settings` must agree, in both directions.

The README's first instruction is `cp .env.example .env`, so this file is the
one a stranger runs into before anything else.

Both directions bite, differently:

* A key in the example with no `Settings` field is a setting that does nothing.
  It reads as configuration, someone sets it, and the value is discarded in
  silence, which is worse than a crash because nothing points at it.

  (Under pydantic-settings' default this WOULD be a startup crash: it ignores an
  unknown environment *variable* but rejects an unknown key in a dotenv *file*.
  This module sets `extra="ignore"`, so the crash never happens here. The
  rationale was copied from a sibling module that does not set it; verified
  empirically rather than reasoned from the library's defaults.)
* A field with no key is a setting nobody discovers. For this example that
  currently includes the one that decides which number the attendant answers on
  — a default that silently answers everywhere would be a surprise, not a
  convenience.
"""

from __future__ import annotations

import pathlib

from support_agent.config import Settings

ENV_EXAMPLE = pathlib.Path(__file__).resolve().parents[1] / ".env.example"


def _example_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip().lower())
    return keys


def test_the_scan_actually_finds_keys() -> None:
    """Guard the guard: an empty scan would make both assertions vacuous."""
    keys = _example_keys()
    assert "turbo_notify_api_key" in keys
    assert len(keys) > 5


def test_every_documented_key_is_a_real_setting() -> None:
    undeclared = sorted(_example_keys() - set(Settings.model_fields))
    assert not undeclared, (
        "these keys are in .env.example and on no Settings field, so copying "
        f"the example to .env — which the README tells you to do — fails to "
        f"start: {undeclared}"
    )


def test_every_setting_is_documented() -> None:
    undocumented = sorted(set(Settings.model_fields) - _example_keys())
    assert not undocumented, (
        f"these settings exist and appear nowhere in .env.example: {undocumented}"
    )
