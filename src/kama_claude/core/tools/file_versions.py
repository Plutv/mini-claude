from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileVersion:
    size: int
    sha256: str


class FileVersionTracker:
    """Track the file version observed by an agent before it overwrites a file."""

    def __init__(self) -> None:
        self._versions: dict[Path, FileVersion] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(path: Path) -> Path:
        return path.resolve()

    @staticmethod
    def _version(content: bytes) -> FileVersion:
        return FileVersion(size=len(content), sha256=hashlib.sha256(content).hexdigest())

    def record(self, path: Path, content: bytes) -> None:
        with self._lock:
            self._versions[self._key(path)] = self._version(content)

    def validate_write(self, path: Path) -> str | None:
        """Return a conflict message when an existing file was not read or changed."""
        if not path.exists():
            return None

        key = self._key(path)
        with self._lock:
            expected = self._versions.get(key)
        if expected is None:
            return f"refusing to overwrite {path}: read_file must be called first"

        current = self._version(path.read_bytes())
        if current != expected:
            return f"refusing to overwrite {path}: file changed after it was read"
        return None
