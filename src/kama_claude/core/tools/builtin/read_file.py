from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.file_versions import FileVersionTracker
from kama_claude.core.tools.workspace import Workspace

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadFileTool(BaseTool):
    read_only = True
    parallel_safe = True
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the current working directory. "
        "Files larger than 512 KB are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file (relative to current working directory).",
            }
        },
        "required": ["path"],
    }

    def __init__(
        self,
        version_tracker: FileVersionTracker | None = None,
        workspace: Path | None = None,
    ) -> None:
        self._version_tracker = version_tracker
        self._workspace = Workspace(workspace)

    # 读取文件内容；超 512KB 截断；禁止 .. 路径遍历
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = ReadFileParams.model_validate(params).path

        path = self._workspace.resolve(path_str)
        raw = await asyncio.to_thread(path.read_bytes)  # raises FileNotFoundError if absent
        if self._version_tracker is not None:
            self._version_tracker.record(path, raw)
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)
