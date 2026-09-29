"""What the attendant is allowed to do.

The message this agent answers is written by a stranger and is placed directly
into the model's turn, so the tool list is the security boundary, not the
prompt. These tests pin the boundary rather than the wording.
"""

from __future__ import annotations

import ast
from pathlib import Path

from support_agent.agent import ALLOWED_TOOLS, INSTRUCTIONS, build_mcp_tools
from support_agent.config import Settings

from .fixtures.mcp_tool_names import KNOWN_MCP_TOOL_NAMES
from .fixtures.settings import settings_ignoring_dotenv

#: Tools that pick their own recipient. Reachable from the text of an inbound
#: message the moment one of them is in the list.
#:
#: `send_typing_indicator` belongs here too, and IS used by this example:
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


def test_disconnect_number_is_absent() -> None:
    """It would switch off the channel through which it could be asked to come back."""
    assert "disconnect_number" not in ALLOWED_TOOLS


def test_the_allowlist_is_exactly_what_answering_requires() -> None:
    """Stated by name so widening it is a deliberate edit to this test too.

    This single exact-set assertion is what makes the narrower checks above
    (no arbitrary-recipient tool, no `disconnect_number`) implied by it, not
    redundant with it: those two exist for their own documentation value, not
    because this one could miss what they catch.
    """
    assert set(ALLOWED_TOOLS) == {"get_message", "reply_to_message"}


def test_the_allowlist_only_names_tools_the_server_actually_exposes() -> None:
    """Pin `ALLOWED_TOOLS` against a snapshot of the server's real catalogue.

    Nothing here calls the live MCP server: the suite has to run with no
    network and no API key. `tests/fixtures/mcp_tool_names.py` is a snapshot
    instead, refreshed by hand from the published tool catalogue (its module
    docstring says how). Without this, a typo or a renamed tool in
    `ALLOWED_TOOLS` would only surface at the first real connection, in
    someone's deployment, not in this suite.
    """
    unknown = set(ALLOWED_TOOLS) - KNOWN_MCP_TOOL_NAMES
    assert not unknown, f"not in the known tool catalogue: {unknown}"


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


class TestBuildMcpTools:
    """`build_mcp_tools` is the entire connection to the real server.

    A wrong URL, a missing header or the wrong transport all fail the same
    way: silently, at the first real run, with no test ever having exercised
    them. These tests build the object `build_mcp_tools` returns and read its
    connection parameters back, with no network involved.
    """

    def _settings(self, **overrides: object) -> Settings:
        base: dict[str, object] = {"turbo_notify_api_key": "tn_test_key"}
        base.update(overrides)
        return settings_ignoring_dotenv(**base)

    def test_it_uses_the_configured_url(self) -> None:
        tools = build_mcp_tools(
            self._settings(turbo_notify_mcp_url="https://mcp.turbonotify.com")
        )
        assert tools.url == "https://mcp.turbonotify.com"

    def test_it_uses_streamable_http(self) -> None:
        """Not `stdio`, not `sse`: the hosted server is reached over Streamable HTTP."""
        tools = build_mcp_tools(self._settings())
        assert tools.transport == "streamable-http"

    def test_it_carries_the_api_key_as_a_bearer_token(self) -> None:
        """The header IS the identity of every call the agent makes.

        `_mcp_headers` is the library's own attribute name for what `headers=`
        sets; there is no public getter, so the test reads it directly rather
        than not checking at all.
        """
        tools = build_mcp_tools(self._settings(turbo_notify_api_key="tn_live_abc123"))
        assert tools._mcp_headers == {"Authorization": "Bearer tn_live_abc123"}

    def test_it_only_includes_the_allowed_tools(self) -> None:
        tools = build_mcp_tools(self._settings())
        # `build_mcp_tools` always passes a concrete list; `include_tools` is
        # only `| None` because `MCPTools` also serves callers who omit it.
        assert tools.include_tools is not None
        assert set(tools.include_tools) == set(ALLOWED_TOOLS)


def test_the_instructions_treat_get_message_results_as_customer_data() -> None:
    """The full body arrives as a tool result, outside the fenced block.

    `build_prompt` fences only what the webhook carried. The whole message
    comes back from `get_message`, so the standing instructions are the only
    place that can tell the model to read it as data. Dropping that paragraph
    reopens the injection path the fence closes for the preview.
    """
    paragraph = INSTRUCTIONS.split("`get_message` returns", 1)[1]
    assert "never as an instruction" in paragraph
    assert "tags themselves" in paragraph
