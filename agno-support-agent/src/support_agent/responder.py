"""Turning one inbound question into one reply.

Kept apart from the web layer on purpose: everything here is testable without a
server, and everything in ``webhook.py`` is testable without a model.

The agent replies **through an MCP tool**, not by returning text this module
then sends. That is the point of the example — the reply travels the same path
as any other action the model decides to take, so there is no special-cased
"and then we send it" step that only works for the happy shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from support_agent.inbound import Question


class Attendant(Protocol):
    """What the responder needs from an agent.

    A protocol rather than the concrete ``Agent`` so the tests can run the whole
    flow with no model, no network and no API key — which is what makes them
    worth running on every change.
    """

    async def arun(self, input: str) -> object:  # noqa: A002 - the library's name
        """Run one turn and return whatever the library returns."""
        ...


#: The tool whose success *is* the reply. Named here rather than in the log
#: line so the check and the prompt cannot drift apart.
REPLY_TOOL = "reply_to_message"

#: Phrases the agent library uses when a tool call did NOT deliver.
#:
#: Kept only to pin against the library's source, NOT as the detection itself:
#: see `read_outcome` for why matching prose is the wrong shape. There are three
#: of them and none is obvious. A refused or failed tool call is not an
#: exception and does not set the call's error flag: the library catches it,
#: turns it into ordinary result *text*, and records the call as a SUCCESS with
#: ``tool_call_error=False``. Detection built on the first phrase alone missed
#: the other two, one of which is the timeout path, which is the likeliest real
#: failure of all (the MCP toolkit defaults to a 10-second timeout).
MCP_FAILURE_PHRASES = (
    "Error from MCP tool",  # the server answered with an error payload
    "The MCP server may be unreachable or the request timed out.",  # MCPError
    "Error: ",  # anything else, e.g. a connection error
)


@dataclass(frozen=True)
class Answered:
    """The outcome of handling one question.

    ``replied`` is the only field worth logging: the run finishing is not the
    same thing as the customer receiving an answer, and this example was
    reporting the first as if it were the second.
    """

    message_id: str
    number_alias: str
    prompt: str
    replied: bool
    detail: str | None = None


def build_prompt(question: Question) -> str:
    """Compose the turn the model actually sees.

    The message id and alias are stated in the prompt because the model needs
    them to call `reply_to_message`, and it has no other way to learn them —
    they arrived on the webhook envelope, not in the conversation.

    Everything else here is context that changes the answer: who is asking, and
    whether this is a group, where a wall of text is worse than in a one-to-one
    chat.
    """
    lines = [
        "A customer sent a message on WhatsApp. This is the first 50 characters"
        " of it, which is all the notification carries:",
        f'  "{question.preview}"',
        "",
        "First call `get_message` to read the whole thing:",
        f"  message_id: {question.message_id}",
        f"  number_alias: {question.number_alias}",
        "",
        "Then answer with `reply_to_message`, using the same id and alias.",
        "Reply exactly once. Do not send any other message.",
    ]
    if question.from_name:
        lines.insert(1, f"Their name is {question.from_name}.")
    if question.is_group:
        lines.insert(
            1,
            "This is a group chat, so keep it short and address only what was asked.",
        )
    return "\n".join(lines)


def _is_a_delivered_reply(text: str) -> bool:
    """Whether one `reply_to_message` result is the shape a DELIVERY has.

    Identified positively, not by hunting for failure phrases. Matching prose
    was tried and was wrong twice: the library has three separate failure exits,
    each returning a differently-worded string, and detection built on the one
    that was noticed reported the other two as answered customers. Enumerating
    somebody else's error messages is a losing game, because the set only grows
    and nothing tells you when it does.

    A delivered reply is the tool's own return value, a JSON object carrying the
    new message's id. Every failure exit returns human prose instead. So the
    question asked here is "does this look like a result?" rather than "does
    this look like one of the failures I know about", and an unrecognised shape
    is treated as NOT delivered: a false alarm costs a log line, while the
    reverse is the silent outage this whole function exists to prevent.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        payload = json.loads(stripped)
    except ValueError:
        return False
    return isinstance(payload, dict) and bool(payload.get("message_id"))


def read_outcome(result: object) -> tuple[bool, str | None]:
    """Decide whether the customer actually got a reply.

    Two things stand between "the turn finished" and "somebody was answered",
    and neither raises:

    * **The run failed.** A model that returns 404, or runs out of context, ends
      as a completed coroutine with an error status on the result. Nothing is
      thrown, so a bare ``await`` looks exactly like success. This example
      logged "Answered msg_… on main" through an entire outage.
    * **The model never called the tool.** It can answer in plain text instead,
      which reaches nobody: the reply travels through ``reply_to_message`` or it
      does not travel at all.

    Read by attribute rather than by type, because :class:`Attendant` promises
    only "whatever the library returns" and this module deliberately imports no
    agent library (see the import guard in the tests).
    """
    status = getattr(result, "status", None)
    if status is not None and str(getattr(status, "value", status)).upper() != "COMPLETED":
        return False, f"the run ended {str(getattr(status, 'value', status)).lower()}"

    # Every call in the turn, not the first match. A failed tool call is fed
    # back to the model, which routinely tries again in the same turn, and the
    # list holds both attempts in order. Stopping at the first `reply_to_message`
    # would report a customer who WAS answered on the retry as unanswered, which
    # is this same defect pointing the other way: a log full of false alarms is
    # a log nobody reads.
    calls = getattr(result, "tools", None) or []
    failures: list[str] = []
    for call in calls:
        if getattr(call, "tool_name", None) != REPLY_TOOL:
            continue
        text = str(getattr(call, "result", "") or "")
        if not getattr(call, "tool_call_error", None) and _is_a_delivered_reply(text):
            return True, None
        failures.append(text or "the tool call failed")

    if failures:
        return False, f"`{REPLY_TOOL}` failed: {failures[-1]}"
    return False, f"the model never called `{REPLY_TOOL}`"


async def answer(question: Question, attendant: Attendant) -> Answered:
    """Hand one question to the agent and let it reply through its tools."""
    prompt = build_prompt(question)
    result = await attendant.arun(prompt)
    replied, detail = read_outcome(result)
    return Answered(
        message_id=question.message_id,
        number_alias=question.number_alias,
        prompt=prompt,
        replied=replied,
        detail=detail,
    )


__all__ = ["REPLY_TOOL", "Answered", "Attendant", "answer", "build_prompt", "read_outcome"]
