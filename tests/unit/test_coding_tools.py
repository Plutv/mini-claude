from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.edit_file import EditFileTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.search_text import SearchTextTool
from kama_claude.core.tools.file_versions import FileVersionTracker


async def test_search_text_returns_bounded_path_and_line_matches(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "def parse():\n    return 'needle'\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "other.md").write_text("needle", encoding="utf-8")

    result = await SearchTextTool(tmp_path).invoke(
        {"query": "NEEDLE", "path": "src", "glob": "*.py"}
    )

    assert not result.is_error
    assert "src/app.py:2:" in result.content
    assert "other.md" not in result.content


async def test_search_text_enforces_result_limit(tmp_path: Path) -> None:
    (tmp_path / "many.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")

    result = await SearchTextTool(tmp_path).invoke(
        {"query": "hit", "max_results": 2}
    )

    assert result.content.count("many.txt:") == 2
    assert result.content.endswith("[results truncated]")


async def test_edit_file_requires_exact_occurrence_count(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")

    result = await EditFileTool(workspace=tmp_path).invoke(
        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}
    )

    assert result.is_error
    assert result.error_type == "conflict"
    assert target.read_text(encoding="utf-8") == "value = 1\nvalue = 1\n"


async def test_edit_file_uses_read_version_and_updates_atomically(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    versions = FileVersionTracker()
    await ReadFileTool(versions, tmp_path).invoke({"path": "app.py"})

    result = await EditFileTool(versions, tmp_path).invoke(
        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}
    )

    assert not result.is_error
    assert "sha256=" in result.content
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert not list(tmp_path.glob(".*.tmp"))


async def test_edit_file_rejects_change_after_read(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    versions = FileVersionTracker()
    await ReadFileTool(versions, tmp_path).invoke({"path": "app.py"})
    target.write_text("value = external\n", encoding="utf-8")

    result = await EditFileTool(versions, tmp_path).invoke(
        {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}
    )

    assert result.is_error
    assert result.error_type == "conflict"
    assert target.read_text(encoding="utf-8") == "value = external\n"


async def test_edit_file_replace_failure_preserves_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    def fail_replace(source: Path, destination: Path) -> None:
        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr("kama_claude.core.tools.builtin.edit_file.os.replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        await EditFileTool(workspace=tmp_path).invoke(
            {"path": "app.py", "old_text": "value = 1", "new_text": "value = 2"}
        )

    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert not list(tmp_path.glob(".*.tmp"))
