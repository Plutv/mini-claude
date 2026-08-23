from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.file_versions import FileVersionTracker
from kama_claude.core.tools.workspace import Workspace

_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    content: str


class WriteFileTool(BaseTool):
    params_model = WriteFileParams
    name = "write_file"
    description = (
        "Write text content to a file, creating it (and any parent directories) if it "
        "does not exist, or overwriting it if it does. "
        "Path must be relative to the current working directory. "
        "Content size is limited to 1 MB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            },
            "content": {
                "type": "string",
                "description": "Text content to write.",
            },
        },
        "required": ["path", "content"],
    }

    def __init__(
        self,
        version_tracker: FileVersionTracker | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._version_tracker = version_tracker
        self._workspace = Workspace(workspace)

    # 写入文件内容；超 1MB 拒绝；禁止 .. 路径遍历；自动创建父目录
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path_str = p.path
        content = p.content

        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        path = self._workspace.resolve(path_str)
        if self._version_tracker is not None:
            conflict = await asyncio.to_thread(self._version_tracker.validate_write, path)
            if conflict is not None:
                return ToolResult(content=conflict, is_error=True, error_type="conflict")

        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)

        def _atomic_write() -> None:
            temporary = path.parent / (
                f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
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

        await asyncio.to_thread(_atomic_write)
        if self._version_tracker is not None:
            self._version_tracker.record(path, encoded)

        return ToolResult(content=f"wrote {len(encoded)} bytes to {path_str}")
