from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.workspace import Workspace


class ReadArtifactParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=20_000, ge=1, le=100_000)


class ReadArtifactTool(BaseTool):
    read_only = True
    parallel_safe = True
    params_model = ReadArtifactParams
    name = "read_artifact"
    description = (
        "Read a bounded slice of a large tool result previously externalized as an "
        "artifact. Only files from the current run's artifact directory are accessible."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Artifact path from a tool result."},
            "offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 100000},
        },
        "required": ["path"],
    }

    def __init__(self, artifact_directory: Path) -> None:
        artifact_directory.mkdir(parents=True, exist_ok=True)
        self._workspace = Workspace(artifact_directory)

    def _read(self, params: ReadArtifactParams) -> ToolResult:
        path = self._workspace.resolve(params.path)
        if path.suffix != ".txt":
            raise PermissionError("only artifact text payloads may be read")
        content = path.read_text(encoding="utf-8", errors="replace")
        end = min(len(content), params.offset + params.max_chars)
        chunk = content[params.offset:end]
        header = f"[artifact chars {params.offset}:{end} of {len(content)}]"
        suffix = "\n[more available]" if end < len(content) else ""
        return ToolResult(content=f"{header}\n{chunk}{suffix}")

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = ReadArtifactParams.model_validate(params)
        return await asyncio.to_thread(self._read, parsed)
