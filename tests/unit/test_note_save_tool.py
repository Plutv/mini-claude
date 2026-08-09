from __future__ import annotations

from pathlib import Path

from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin.note_save import NoteSaveTool


# 功能：验证 note_save 正常调用会把 content 写入 notes.md
# 设计：使用真实 SessionStore 和 tmp_path，断言工具返回与文件内容，覆盖工具到文件层的完整路径
async def test_note_save_appends_note(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    tool = NoteSaveTool(store, "sess-1", "run-1")

    result = await tool.invoke({"content": "Python 3.12"})

    assert result.content == "saved"
    assert not result.is_error
    assert "Python 3.12" in store.read_notes("sess-1")


# 功能：验证空 content 会返回工具错误且不写入 notes.md
# 设计：传入空白字符串，断言 is_error 与 error_type，覆盖 schema 之外的业务校验
async def test_note_save_rejects_empty_content(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    tool = NoteSaveTool(store, "sess-1", "run-1")

    result = await tool.invoke({"content": "   "})

    assert result.is_error
    assert result.error_type == "runtime_error"
    assert store.read_notes("sess-1") == ""


async def test_note_save_writes_scoped_searchable_memory(tmp_path: Path) -> None:
    session_store = SessionStore(tmp_path / "sessions")
    memory_store = MemoryStore(tmp_path / "memory.sqlite3")
    tool = NoteSaveTool(
        session_store,
        "sess-1",
        "run-1",
        memory_store=memory_store,
        project_scope="project:demo",
    )

    result = await tool.invoke(
        {
            "content": "This repository uses ruff formatting",
            "scope": "project",
            "tags": ["python", "format"],
            "importance": 0.8,
        }
    )

    recalled = memory_store.recall("format Python", scopes=["project:demo"])
    assert not result.is_error
    assert len(recalled) == 1
    assert recalled[0].content == "This repository uses ruff formatting"
    assert session_store.read_notes("sess-1") == ""
