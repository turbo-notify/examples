"""A snapshot of the Turbo Notify MCP server's real tool names.

`ALLOWED_TOOLS` in `agent.py` is checked against this list, not merely against
its own hardcoded contents, so widening it to a name the server does not
actually expose fails here instead of failing silently at the first real
connection.

**How to refresh this list**, after a tool is added, renamed or removed on the
server: either read the tool catalogue table in
<https://docs.turbonotify.com/general/mcp-server/#ferramentas>, or connect to
a running server and call `tools/list` yourself, for example:

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        "https://mcp.turbonotify.com/mcp",
        headers={"Authorization": "Bearer <your key>"},
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = sorted(t.name for t in (await session.list_tools()).tools)

Then replace the frozenset below with the result.
"""

from __future__ import annotations

#: 32 tools, as of the tool catalogue documented at
#: <https://docs.turbonotify.com/general/mcp-server/>.
KNOWN_MCP_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # Sending.
        "send_text",
        "send_media",
        "send_location",
        "send_contact_card",
        "send_calendar_event",
        "send_cta_button",
        # Acting on a message.
        "reply_to_message",
        "edit_message",
        "delete_message",
        "react_to_message",
        "send_typing_indicator",
        "mark_as_read",
        # Reading messages.
        "list_messages",
        "get_message",
        "get_message_status",
        "get_message_receipts",
        "get_message_reactions",
        # Contacts.
        "list_contacts",
        "get_contact",
        "get_contact_profile_picture",
        "refresh_contact",
        # Groups.
        "list_groups",
        "get_group",
        "get_group_picture",
        # Numbers.
        "list_numbers",
        "get_number",
        "connect_number",
        "disconnect_number",
        # Events.
        "list_events",
        # Usage and quotas.
        "get_usage",
        "get_message_quota",
        "get_contact_quota",
    }
)

__all__ = ["KNOWN_MCP_TOOL_NAMES"]
