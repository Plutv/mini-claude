from __future__ import annotations

import asyncio
import fnmatch
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.workspace import Workspace

_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules"})
_MAX_FILE_BYTES = 2 * 1024 * 1024


class SearchTextParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    query: str = Field(min_length=1, max_length=500)
    path: str = "."
    glob: str = "*"
    max_results: int = Field(default=50, ge=1, le=200)
    case_sensitive: bool = False


class SearchTextTool(BaseTool):
    read_only = True
    parallel_safe = True
    params_model = SearchTextParams
    name = "search_text"
    description = (
        "Search repository text files and return bounded path:line matches. "
        "Use this before reading whole files. Paths are restricted to the session workspace."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Literal text to search for."},
            "path": {
                "type": "string",
                "description": "Relative file or directory to search (default '.').",
            },
            "glob": {
                "type": "string",
                "description": "Filename glob such as '*.py' (default '*').",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
            },
            "case_sensitive": {"type": "boolean"},
        },
        "required": ["query"],
    }

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = Workspace(workspace)

    def _search(self, params: SearchTextParams) -> str:
        target = self._workspace.resolve(params.path)
        if not target.exists():
            raise FileNotFoundError(f"no such path: {params.path}")

        files = [target] if target.is_file() else target.rglob("*")
        needle = params.query if params.case_sensitive else params.query.casefold()
        matches: list[str] = []
        scanned = 0
        skipped = 0
        for path in files:
            if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
                continue
            if not fnmatch.fnmatch(path.name, params.glob):
                continue
            try:
                raw = path.read_bytes()
            except OSError:
                skipped += 1
                continue
            if len(raw) > _MAX_FILE_BYTES or b"\x00" in raw:
                skipped += 1
                continue
            scanned += 1
            text = raw.decode("utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                haystack = line if params.case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                display = (
                    path.relative_to(self._workspace.root)
                    if self._workspace.root is not None
                    else path
                )
                matches.append(f"{display}:{line_no}: {line[:500]}")
                if len(matches) >= params.max_results:
                    return "\n".join(matches) + "\n[results truncated]"

        if matches:
            return "\n".join(matches)
        return f"[no matches; scanned={scanned}, skipped={skipped}]"

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = SearchTextParams.model_validate(params)
        return ToolResult(content=await asyncio.to_thread(self._search, parsed))

