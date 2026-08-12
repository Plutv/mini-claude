from __future__ import annotations

from pathlib import Path


class WorkspaceEscapeError(ValueError):
    pass


def resolve_workspace_path(root: Path, requested: str | Path) -> Path:
    root_text = str(root.resolve())
    candidate = (root / requested).resolve()
    if not str(candidate).startswith(root_text):
        raise WorkspaceEscapeError(str(requested))
    return candidate
