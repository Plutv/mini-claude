from __future__ import annotations

import pytest

from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.file_versions import FileVersionTracker


@pytest.mark.asyncio
async def test_existing_file_must_be_read_before_overwrite(tmp_path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("original", encoding="utf-8")
    writer = WriteFileTool(FileVersionTracker())

    result = await writer.invoke({"path": str(target), "content": "agent edit"})

    assert result.is_error is True
    assert result.error_type == "conflict"
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.asyncio
async def test_read_then_write_updates_observed_version(tmp_path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("original", encoding="utf-8")
    tracker = FileVersionTracker()
    reader = ReadFileTool(tracker)
    writer = WriteFileTool(tracker)

    await reader.invoke({"path": str(target)})
    first = await writer.invoke({"path": str(target), "content": "first edit"})
    second = await writer.invoke({"path": str(target), "content": "second edit"})

    assert first.is_error is False
    assert second.is_error is False
    assert target.read_text(encoding="utf-8") == "second edit"


@pytest.mark.asyncio
async def test_external_change_after_read_blocks_stale_write(tmp_path) -> None:
    target = tmp_path / "config.txt"
    target.write_text("original", encoding="utf-8")
    tracker = FileVersionTracker()
    reader = ReadFileTool(tracker)
    writer = WriteFileTool(tracker)

    await reader.invoke({"path": str(target)})
    target.write_text("changed by user", encoding="utf-8")
    result = await writer.invoke({"path": str(target), "content": "stale agent edit"})

    assert result.is_error is True
    assert result.error_type == "conflict"
    assert "changed after it was read" in result.content
    assert target.read_text(encoding="utf-8") == "changed by user"


@pytest.mark.asyncio
async def test_new_file_does_not_require_prior_read(tmp_path) -> None:
    target = tmp_path / "new.txt"
    writer = WriteFileTool(FileVersionTracker())

    result = await writer.invoke({"path": str(target), "content": "created"})

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "created"
