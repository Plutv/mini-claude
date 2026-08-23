from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.list_dir import ListDirTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool


class _UnusedRunner:
    async def run_and_capture(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("runner must not be called")


async def test_file_tools_resolve_relative_paths_inside_workspace(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('old')", encoding="utf-8")

    read = await ReadFileTool(workspace=tmp_path).invoke({"path": "src/app.py"})
    listing = await ListDirTool(tmp_path).invoke({"path": "src", "max_depth": 1})
    write = await WriteFileTool(workspace=tmp_path).invoke(
        {"path": "src/new.py", "content": "answer = 42"}
    )

    assert read.content == "print('old')"
    assert "app.py" in listing.content
    assert not write.is_error
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "answer = 42"


async def test_file_tools_reject_absolute_and_parent_workspace_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(PermissionError):
        await ReadFileTool(workspace=workspace).invoke({"path": str(outside)})
    with pytest.raises(PermissionError):
        await WriteFileTool(workspace=workspace).invoke(
            {"path": "../outside.txt", "content": "overwrite"}
        )

    assert outside.read_text(encoding="utf-8") == "secret"


async def test_file_tools_reject_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    try:
        (workspace / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")

    with pytest.raises(PermissionError):
        await ReadFileTool(workspace=workspace).invoke({"path": "link/secret.txt"})


async def test_bash_starts_in_bound_workspace(tmp_path: Path) -> None:
    result = await BashTool(tmp_path).invoke({"command": "pwd"})

    assert not result.is_error
    assert Path(result.content.strip()).resolve() == tmp_path.resolve()


async def test_session_persists_normalized_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    manager = SessionManager(
        store,
        lambda: _UnusedRunner(),  # type: ignore[arg-type]
        EventBus(),
    )

    session = await manager.create("chat", workspace=str(workspace / "."))

    assert session.workspace == str(workspace.resolve())
    assert store.read_meta(session.id).workspace == str(workspace.resolve())
