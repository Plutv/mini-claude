from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.file_versions import FileVersionTracker
from kama_claude.core.tools.workspace import Workspace

_MAX_BYTES = 1 * 1024 * 1024


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    old_text: str = Field(min_length=1)
    new_text: str
    expected_replacements: int = Field(default=1, ge=1, le=1000)


class EditFileTool(BaseTool):
    params_model = EditFileParams
    name = "edit_file"
    description = (
        "Replace an exact text fragment in an existing UTF-8 file. The edit is rejected "
        "when the expected occurrence count or previously-read file version changed, and "
        "is committed with an atomic replace. Prefer this over rewriting a whole file."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "old_text": {"type": "string", "description": "Exact text to replace."},
            "new_text": {"type": "string", "description": "Replacement text."},
            "expected_replacements": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "description": "Required occurrence count (default 1).",
            },
        },
        "required": ["path", "old_text", "new_text"],
    }

    def __init__(
        self,
        version_tracker: FileVersionTracker | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._version_tracker = version_tracker
        self._workspace = Workspace(workspace)

    def _edit(self, params: EditFileParams) -> ToolResult:
        path = self._workspace.resolve(params.path)
        raw = path.read_bytes()
        if self._version_tracker is not None:
            conflict = self._version_tracker.validate_write(path)
            if conflict is not None:
                return ToolResult(content=conflict, is_error=True, error_type="conflict")

        try:
            original = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                content=f"file is not valid UTF-8: {params.path}",
                is_error=True,
                error_type="schema_error",
            )
        occurrences = original.count(params.old_text)
        if occurrences != params.expected_replacements:
            return ToolResult(
                content=(
                    f"edit precondition failed: expected "
                    f"{params.expected_replacements} occurrence(s), found {occurrences}"
                ),
                is_error=True,
                error_type="conflict",
            )

        updated = original.replace(
            params.old_text,
            params.new_text,
            params.expected_replacements,
        )
        encoded = updated.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"edited content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                temporary.chmod(path.stat().st_mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

        if self._version_tracker is not None:
            self._version_tracker.record(path, encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        return ToolResult(
            content=(
                f"edited {params.path}: {params.expected_replacements} replacement(s), "
                f"{len(encoded)} bytes, sha256={digest}"
            )
        )

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = EditFileParams.model_validate(params)
        return await asyncio.to_thread(self._edit, parsed)

