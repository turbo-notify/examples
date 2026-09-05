"""The reference material the attendant answers from.

A single markdown file, loaded into the agent's context. Not a vector store,
and that is a decision rather than a shortcut.

Turbo Notify's public documentation is a few dozen short pages. Loaded whole it
costs a fixed amount of context per run and the model sees all of it, every
time — no retrieval step to tune, no embedding provider to pay, no vector
database to run, and no class of failure where the right passage simply was not
retrieved. For a corpus this size that is strictly better.

**When to graduate to retrieval.** Roughly: when the material stops fitting
comfortably in context, when it changes often enough that a rebuild-and-redeploy
is too slow, or when answers need customer-specific documents rather than one
shared set. Agno has `Knowledge` with a vector database for exactly that — swap
`description=load_knowledge()` in agent.py for `knowledge=...` and keep
everything else. Doing it before you need it buys nothing and costs a service.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from support_agent.config import PROJECT_ROOT

KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"


@lru_cache
def load_knowledge() -> str:
    """Read every markdown file in ``knowledge/``, in a stable order.

    Sorted by filename so two runs produce byte-identical context. That matters
    for prompt caching: an unstable prefix invalidates the cache on every run
    and quietly multiplies the cost of the whole thing.
    """
    if not KNOWLEDGE_DIR.is_dir():
        raise FileNotFoundError(
            f"No knowledge directory at {KNOWLEDGE_DIR}. The attendant has "
            f"nothing to answer from."
        )

    documents = sorted(KNOWLEDGE_DIR.glob("*.md"))
    if not documents:
        raise FileNotFoundError(
            f"No markdown files in {KNOWLEDGE_DIR}. The attendant has nothing to answer from."
        )

    parts = [
        "The following is the reference material about Turbo Notify. "
        "Answer from it. If it does not cover something, say so.",
        "",
    ]
    for document in documents:
        parts.append(f"--- {document.name} ---")
        parts.append(document.read_text(encoding="utf-8").strip())
        parts.append("")
    return "\n".join(parts)


def knowledge_documents() -> list[Path]:
    """The files that make up the reference material."""
    return sorted(KNOWLEDGE_DIR.glob("*.md"))


__all__ = ["KNOWLEDGE_DIR", "knowledge_documents", "load_knowledge"]
