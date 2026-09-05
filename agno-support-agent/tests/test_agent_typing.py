"""Showing "typing…" by calling the MCP server directly, never through the model.

`send_typing_indicator` takes an arbitrary recipient — the same shape every
other excluded tool has (see `test_agent_tools.py`). What makes it safe to use
at all is that nothing here ever hands the model a chance to choose that
recipient: `AgnoAttendant.show_typing` computes it from the verified webhook
sender and calls the raw MCP session directly.
"""

from __future__ import annotations

from typing import Any

from support_agent.agent import AgnoAttendant, _typing_arguments
from support_agent.inbound import Question


def _question(**overrides: Any) -> Question:
    base: dict[str, Any] = {
        "event_id": "evt_" + "a" * 32,
        "message_id": "msg_" + "a" * 32,
        "number_alias": "main",
        "content_type": "text",
        "preview": "How do quotas work?",
        "from_name": "Ana",
        "is_group": False,
        "group_id": None,
        "from_number": "5511999999999",
    }
    base.update(overrides)
    return Question(**base)


class TestTypingArguments:
    def test_a_one_to_one_question_targets_the_sender(self) -> None:
        assert _typing_arguments(_question()) == {"number": "5511999999999"}

    def test_a_group_question_targets_the_group_not_a_member(self) -> None:
        args = _typing_arguments(
            _question(is_group=True, group_id="grp_" + "b" * 32, from_number="5511999999999")
        )
        assert args == {"group_id": "grp_" + "b" * 32}

    def test_neither_identifier_present_is_unusable(self) -> None:
        """Enough to build a `Question` at all is not enough to type-indicate."""
        assert _typing_arguments(_question(from_number=None)) is None


class _FakeResult:
    def __init__(self, *, is_error: bool = False) -> None:
        self.is_error = is_error


class _FakeSession:
    def __init__(self, *, is_error: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._is_error = is_error

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> _FakeResult:
        self.calls.append((name, arguments))
        return _FakeResult(is_error=self._is_error)


class _FakeMcpTools:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.session_requests = 0

    async def get_session_for_run(self) -> _FakeSession:
        self.session_requests += 1
        return self._session


class TestAgnoAttendantShowTyping:
    async def test_it_calls_send_typing_indicator_with_the_computed_recipient(self) -> None:
        session = _FakeSession()
        attendant = AgnoAttendant(agent=None, mcp_tools=_FakeMcpTools(session))  # type: ignore[arg-type]

        await attendant.show_typing(_question(number_alias="support"))

        assert len(session.calls) == 1
        name, arguments = session.calls[0]
        assert name == "send_typing_indicator"
        assert arguments == {
            "number": "5511999999999",
            "number_alias": "support",
            "is_typing": True,
        }

    async def test_a_group_question_sends_group_id_not_a_bare_number(self) -> None:
        session = _FakeSession()
        attendant = AgnoAttendant(agent=None, mcp_tools=_FakeMcpTools(session))  # type: ignore[arg-type]

        await attendant.show_typing(_question(is_group=True, group_id="grp_" + "c" * 32))

        assert session.calls[0][1]["group_id"] == "grp_" + "c" * 32
        assert "number" not in session.calls[0][1]

    async def test_no_usable_recipient_skips_the_call_entirely(self) -> None:
        """Not just a no-op reply from the server: no round trip at all."""
        session = _FakeSession()
        mcp_tools = _FakeMcpTools(session)
        attendant = AgnoAttendant(agent=None, mcp_tools=mcp_tools)  # type: ignore[arg-type]

        await attendant.show_typing(_question(from_number=None))

        assert mcp_tools.session_requests == 0
        assert session.calls == []

    async def test_a_declined_call_does_not_raise(self) -> None:
        """The server saying no (plan without the feature, say) is not this

        example's problem to escalate: it is cosmetic feedback either way.
        """
        session = _FakeSession(is_error=True)
        attendant = AgnoAttendant(agent=None, mcp_tools=_FakeMcpTools(session))  # type: ignore[arg-type]

        await attendant.show_typing(_question())  # must not raise
