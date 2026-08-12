from __future__ import annotations

from pathlib import Path


class WorkspaceEscapeError(ValueError):
    pass


def resolve_workspace_path(root: Path, requested: str | Path) -> Path:
    resolved_root = root.resolve()
    raw_requested = Path(requested)
    candidate = (
        raw_requested.resolve()
        if raw_requested.is_absolute()
        else (resolved_root / raw_requested).resolve()
    )
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceEscapeError(str(requested)) from exc
    return candidate
