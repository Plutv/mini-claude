from pathlib import Path

import pytest

from app.workspace import WorkspaceEscapeError, resolve_workspace_path


def test_sibling_with_same_prefix_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    sibling = tmp_path / "workspace-other"
    root.mkdir()
    sibling.mkdir()
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(root, sibling / "secret.txt")


def test_absolute_external_path_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    external = tmp_path / "external.txt"
    root.mkdir()
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(root, external)


def test_root_itself_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    assert resolve_workspace_path(root, ".") == root.resolve()


def test_symlink_target_outside_workspace_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    external = tmp_path / "external"
    root.mkdir()
    external.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(WorkspaceEscapeError):
        resolve_workspace_path(root, "linked/secret.txt")
