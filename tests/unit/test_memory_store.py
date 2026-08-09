from __future__ import annotations

from pathlib import Path

from kama_claude.core.memory.store import MemoryStore, project_memory_scope


def test_recall_ranks_relevant_memory_and_respects_scope(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.save(
        scope="global",
        content="User prefers ruff for Python formatting",
        tags=["python", "formatter"],
        importance=0.9,
    )
    store.save(scope="global", content="Database is PostgreSQL", importance=0.9)
    store.save(scope="session:other", content="Use black for Python formatting", importance=1.0)

    recalled = store.recall(
        "format this Python project",
        scopes=["global", "session:current"],
        limit=2,
    )

    assert [item.content for item in recalled] == ["User prefers ruff for Python formatting"]
    assert recalled[0].score > 0


def test_save_is_idempotent_and_merges_tags(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    first = store.save(scope="global", content="Prefer concise answers", tags=["style"])
    second = store.save(
        scope="global",
        content="Prefer concise answers",
        tags=["preference"],
        importance=0.9,
    )

    assert first.id == second.id
    assert second.tags == ("preference", "style")
    assert second.importance == 0.9


def test_project_scope_is_stable_and_path_specific(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert project_memory_scope(first) == project_memory_scope(first)
    assert project_memory_scope(first) != project_memory_scope(second)


def test_recall_enforces_character_budget(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.sqlite3")
    store.save(scope="global", content="python " + "a" * 80, importance=1.0)
    store.save(scope="global", content="python " + "b" * 80, importance=0.9)

    recalled = store.recall("python", scopes=["global"], limit=5, max_chars=100)

    assert len(recalled) == 1
