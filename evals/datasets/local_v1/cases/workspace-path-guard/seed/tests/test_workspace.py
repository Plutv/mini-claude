from pathlib import Path

import pytest

from app.workspace import WorkspaceEscapeError, resolve_workspace_path


def test_internal_path_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    assert resolve_workspace_path(root, "src/app.py") == root / "src" / "app.py"


def test_parent_traversal_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(root, "../secret.txt")
