from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.file_versions import FileVersionTracker
from kama_claude.core.tools.workspace import Workspace

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    start_line: int | None = Field(default=None, ge=1)
    num_lines: int = Field(default=80, ge=1, le=200)


class ReadFileTool(BaseTool):
    read_only = True
    parallel_safe = True
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file. "
        "Path must be relative to the workspace. For code found by grep_search, "
        "pass its line number as start_line and use num_lines to read a bounded "
        "region instead of the entire file. Whole-file results larger than 512 KB "
        "are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path to the file.",
            },
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional 1-based first line to read, such as a grep_search match.",
            },
            "num_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Lines to return when start_line is set (default 80).",
            },
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
        parsed = ReadFileParams.model_validate(params)

        path = self._workspace.resolve(parsed.path)
        raw = await asyncio.to_thread(path.read_bytes)  # raises FileNotFoundError if absent
        if self._version_tracker is not None:
            self._version_tracker.record(path, raw)
        if parsed.start_line is not None:
            lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
            if parsed.start_line > len(lines):
                return ToolResult(
                    content=f"start_line {parsed.start_line} exceeds file length {len(lines)}",
                    is_error=True,
                    error_type="out_of_range",
                )
            end_line = min(len(lines), parsed.start_line + parsed.num_lines - 1)
            region = "".join(lines[parsed.start_line - 1 : end_line])
            encoded_region = region.encode("utf-8")
            if len(encoded_region) > _MAX_BYTES:
                region = encoded_region[:_MAX_BYTES].decode("utf-8", errors="replace")
                region += "\n[truncated]"
            return ToolResult(
                content=f"[file lines {parsed.start_line}:{end_line} of {len(lines)}]\n{region}"
            )
        truncated = len(raw) > _MAX_BYTES
        text = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
        if truncated:
            text += "\n[truncated]"

        return ToolResult(content=text)
