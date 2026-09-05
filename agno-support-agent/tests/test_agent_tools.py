"""What the attendant is allowed to do.

The message this agent answers is written by a stranger and is placed directly
into the model's turn, so the tool list is the security boundary, not the
prompt. These tests pin the boundary rather than the wording.
"""

from __future__ import annotations

import ast
from pathlib import Path

from support_agent.agent import ALLOWED_TOOLS

#: Tools that pick their own recipient. Reachable from the text of an inbound
#: message the moment one of them is in the list.
#:
#: `send_typing_indicator` belongs here too, and IS used by this example —
#: `AgnoAttendant.show_typing` (`agent.py`) calls it directly through the raw
#: MCP session, with a recipient THIS CODE computed from the verified
#: webhook sender, never through the model's own tool-calling loop. That is
#: what keeps it safe despite having exactly the shape this set exists to
#: keep off `ALLOWED_TOOLS`.
ARBITRARY_RECIPIENT_TOOLS = frozenset(
    {
        "send_text",
        "send_media",
        "send_location",
        "send_contact_card",
        "send_calendar_event",
        "send_cta_button",
        "send_typing_indicator",
    }
)


def test_no_tool_that_chooses_its_own_recipient_is_allowed() -> None:
    assert ARBITRARY_RECIPIENT_TOOLS.isdisjoint(ALLOWED_TOOLS)


def test_the_only_write_is_replying_to_the_message_it_was_handed() -> None:
    """`reply_to_message` cannot address anyone the agent was not already talking to."""
    writes = {t for t in ALLOWED_TOOLS if not t.startswith(("list_", "get_"))}
    assert writes == {"reply_to_message"}


def test_disconnect_number_is_absent() -> None:
    """It would switch off the channel through which it could be asked to come back."""
    assert "disconnect_number" not in ALLOWED_TOOLS


def test_no_listing_tool_is_allowed() -> None:
    """A listing enumerates an inbox; a fetch by id cannot.

    An API key is scoped to the ORGANIZATION, not to a conversation, so
    `list_messages`/`list_contacts`/`list_groups` return everything for any
    number that key reaches, with the alias chosen by the model. Those are
    aimable by a crafted message and stay out. `get_message` takes one
    unguessable id, which the agent was handed by the notification it is
    answering, and without it there is nothing to answer with: the webhook
    carries a 50-character preview, never the body.
    """
    listings = {t for t in ALLOWED_TOOLS if t.startswith("list_")}
    assert listings == set()


def test_the_allowlist_is_exactly_what_answering_requires() -> None:
    """Stated by name so widening it is a deliberate edit to this test too."""
    assert set(ALLOWED_TOOLS) == {"get_message", "reply_to_message"}


def test_the_toolset_is_an_allowlist_not_a_blocklist() -> None:
    """Structural, because the difference is the whole point.

    A blocklist admits every tool the server grows next; an allowlist admits
    none of them until someone decides to. Asserting on `ALLOWED_TOOLS`'s
    contents alone would still pass if the call site quietly went back to
    `exclude_tools`, so this reads the call site itself.
    """
    source = Path(__file__).resolve().parents[1] / "src" / "support_agent" / "agent.py"
    tree = ast.parse(source.read_text())

    keywords: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "MCPTools"
        ):
            keywords = {kw.arg for kw in node.keywords if kw.arg}

    assert "include_tools" in keywords, "MCPTools must be given an allowlist"
    assert "exclude_tools" not in keywords, "a blocklist admits every future tool"
