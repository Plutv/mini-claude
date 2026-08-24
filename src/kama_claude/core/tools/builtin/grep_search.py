from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.workspace import Workspace

_SKIP_DIRS = frozenset({".git", ".venv", "__pycache__", "node_modules"})
_MAX_FILE_BYTES = 2 * 1024 * 1024


class GrepMatch:
    """A single matched (or context) line produced by grep_search."""

    __slots__ = ("path", "line", "text", "is_match")

    def __init__(self, path: str, line: int, text: str, is_match: bool) -> None:
        self.path = path
        self.line = line
        self.text = text
        self.is_match = is_match

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "text": self.text,
            "is_match": self.is_match,
        }


class GrepSearchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str = Field(min_length=1, max_length=500)
    path: str = "."
    glob: str | None = None
    max_results: int = Field(default=50, ge=1, le=200)
    context_lines: int = Field(default=0, ge=0, le=5)
    ignore_case: bool = False


class GrepSearchTool(BaseTool):
    read_only = True
    parallel_safe = True
    params_model = GrepSearchParams
    name = "grep_search"
    description = (
        "Search the workspace for a regular expression and return matching lines with "
        "their file path and line number. Prefers the faster `rg` when available, and "
        "falls back to a pure-Python scan. Use this for locating code before reading "
        "whole files. Optional glob, case-insensitivity, and surrounding context."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for (Python `re` syntax).",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative file or directory to search (default '.').",
            },
            "glob": {
                "type": "string",
                "description": "Filename glob such as '*.py' (default: all files).",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Maximum number of lines to return (default 50).",
            },
            "context_lines": {
                "type": "integer",
                "minimum": 0,
                "maximum": 5,
                "description": "Number of context lines to include before/after each match (default 0).",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "Case-insensitive search (default false).",
            },
        },
        "required": ["pattern"],
    }

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = Workspace(workspace)

    # ---- pure helpers (unit-tested) ---------------------------------------

    def _python_grep(
        self,
        target: Path,
        pattern: str,
        *,
        ignore_case: bool,
        glob: str | None,
        max_results: int,
        context_lines: int,
    ) -> list[GrepMatch]:
        try:
            compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc

        files = [target] if target.is_file() else list(target.rglob("*"))
        scanned = 0
        skipped = 0
        results: list[GrepMatch] = []
        for path in files:
            if not path.is_file() or any(part in _SKIP_DIRS for part in path.parts):
                continue
            if glob is not None and not path.match(glob):
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
            lines = text.splitlines()
            display = (
                str(path.relative_to(self._workspace.root))
                if self._workspace.root is not None
                else str(path)
            )
            for idx in range(len(lines)):
                if not compiled.search(lines[idx]):
                    continue
                lo = max(0, idx - context_lines)
                hi = min(len(lines), idx + context_lines + 1)
                for j in range(lo, hi):
                    results.append(
                        GrepMatch(display, j + 1, lines[j], is_match=(j == idx))
                    )
                    if len(results) >= max_results:
                        return results
        return results

    @staticmethod
    def _parse_rg_json_line(line: str) -> GrepMatch | None:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        kind = payload.get("type")
        data = payload.get("data") or {}
        if kind == "match":
            path = (data.get("path") or {}).get("text", "")
            line_no = int(data.get("line_number", 0))
            text = (data.get("lines") or {}).get("text", "")
            display = path
            return GrepMatch(display, line_no, text.rstrip("\n"), is_match=True)
        if kind == "context":
            path = (data.get("path") or {}).get("text", "")
            line_no = int(data.get("line_number", 0))
            text = (data.get("lines") or {}).get("text", "")
            return GrepMatch(path, line_no, text.rstrip("\n"), is_match=False)
        return None

    def _run_rg(
        self,
        target: Path,
        params: GrepSearchParams,
    ) -> list[GrepMatch] | None:
        """Best-effort `rg --json` search. Returns None on any failure."""
        if shutil.which("rg") is None:
            return None
        # rg line-based output is ambiguous on Windows drive-letter paths;
        # --json sidesteps it, but we still avoid rg on nt to keep behavior
        # consistent with the pure-Python fallback the tests exercise.
        if os.name == "nt":
            return None
        cmd = [
            "rg",
            "--json",
            "--hidden",
            "-g",
            "!.git",
        ]
        if params.ignore_case:
            cmd.append("--ignore-case")
        if params.context_lines > 0:
            cmd.extend(["-C", str(params.context_lines)])
        if params.glob:
            cmd.extend(["-g", params.glob])
        cmd.extend(["--", params.pattern, str(target)])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(target) if target.is_dir() else str(target.parent),
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        results: list[GrepMatch] = []
        for raw_line in proc.stdout.splitlines():
            match = self._parse_rg_json_line(raw_line)
            if match is not None:
                results.append(match)
                if len(results) >= params.max_results:
                    break
        return results

    # ---- orchestration ----------------------------------------------------

    def _search(self, params: GrepSearchParams) -> str:
        target = self._workspace.resolve(params.path)
        if not target.exists():
            raise FileNotFoundError(f"no such path: {params.path}")

        matches = self._run_rg(target, params)
        if matches is None:
            matches = self._python_grep(
                target,
                params.pattern,
                ignore_case=params.ignore_case,
                glob=params.glob,
                max_results=params.max_results,
                context_lines=params.context_lines,
            )

        truncated = len(matches) >= params.max_results
        payload = [m.to_dict() for m in matches]
        summary = {
            "matches": payload,
            "count": len(payload),
            "truncated": truncated,
        }
        return json.dumps(summary, ensure_ascii=False, indent=2)

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = GrepSearchParams.model_validate(params)
        try:
            content = await asyncio.to_thread(self._search, parsed)
        except (FileNotFoundError, ValueError) as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="schema_error")
        return ToolResult(content=content)
