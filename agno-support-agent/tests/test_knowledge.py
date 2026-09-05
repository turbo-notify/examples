"""The reference material the attendant answers from."""

from __future__ import annotations

import pathlib

import pytest

from support_agent import knowledge
from support_agent.knowledge import KNOWLEDGE_DIR, knowledge_documents, load_knowledge


def test_the_knowledge_directory_ships_with_the_example() -> None:
    """A clone with no reference material produces an attendant that invents answers."""
    assert KNOWLEDGE_DIR.is_dir()
    assert knowledge_documents(), "no markdown in knowledge/"


def test_it_loads_and_carries_the_substance() -> None:
    text = load_knowledge()
    for topic in ("quota", "webhook", "number_alias", "connected"):
        assert topic in text, f"reference material never mentions {topic!r}"


def test_it_tells_the_model_what_to_do_when_the_material_falls_short() -> None:
    """Otherwise a gap in the docs becomes a confident wrong answer about billing."""
    assert "does not cover" in load_knowledge()


def test_the_order_is_stable(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unstable prefix invalidates prompt caching on every single run.

    Asserted against a corpus this test OWNS, because the shipped one holds a
    single file and every ordering assertion over one item is vacuous. The
    previous two versions were both tautologies: `load_knowledge` is cached, so
    calling it twice compares an object with itself, and `knowledge_documents()`
    already returns `sorted(...)`, so comparing it to its own sort cannot
    differ. Reversing the real load order left the whole file green.

    Written out of alphabetical order on purpose, so a loader that used
    directory order rather than sorting would fail here.
    """
    (tmp_path / "b-second.md").write_text("second document\n", encoding="utf-8")
    (tmp_path / "a-first.md").write_text("first document\n", encoding="utf-8")
    monkeypatch.setattr(knowledge, "KNOWLEDGE_DIR", tmp_path)
    load_knowledge.cache_clear()

    text = load_knowledge()
    assert text.index("a-first.md") < text.index("b-second.md"), (
        "documents are composed out of filename order, so the prompt prefix "
        "changes between runs and prompt caching never hits"
    )

    load_knowledge.cache_clear()
    assert load_knowledge() == text

    load_knowledge.cache_clear()
