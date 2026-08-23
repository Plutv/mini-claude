from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workspace:
    """Resolve tool paths inside one durable project boundary."""

    root: Path | None = None

    def __post_init__(self) -> None:
        if self.root is None:
            return
        resolved = self.root.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace is not a directory: {resolved}")
        object.__setattr__(self, "root", resolved)

    def resolve(self, raw_path: str) -> Path:
        path = Path(raw_path).expanduser()
        if self.root is None:
            if ".." in path.parts:
                raise PermissionError(f"path traversal not allowed: {raw_path}")
            return path

        candidate = path if path.is_absolute() else self.root / path
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise PermissionError(
                f"path escapes workspace: {raw_path} (workspace={self.root})"
            )
        return resolved

