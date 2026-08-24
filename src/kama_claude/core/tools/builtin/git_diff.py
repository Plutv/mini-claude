from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 256 * 1024  # 256 KB

_SECRET_RE = re.compile(
    r"(api_key|apikey|secret|token|password|passwd|AKIA)\s*[=:]\s*['\"]?[A-Za-z0-9/+]{8,}",
    re.IGNORECASE,
)
_BREAKPOINT_RE = re.compile(r"\b(breakpoint\s*\(|import\s+pdb|console\.log|debugger)\b")
_FILES_CHANGED_RE = re.compile(r"^diff --git ", re.MULTILINE)
_WS_ONLY_RE = re.compile(r"^[+-]\s*$")


class GitDiffParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    staged: bool = False
    path: str | None = None


class GitDiffTool(BaseTool):
    read_only = True
    parallel_safe = True
    params_model = GitDiffParams
    name = "git_diff"
    description = (
        "Show a git diff (working tree or staged) for review, and run lightweight "
        "heuristics over it: leaked secrets, leftover debug statements, large pure-"
        "whitespace reformatting, and an excessive number of unrelated files."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "Show staged changes (git diff --staged) instead of the working tree.",
            },
            "path": {
                "type": "string",
                "description": "Restrict the diff to this path (optional).",
            },
        },
        "required": [],
    }

    def __init__(self, workspace: Path | None = None) -> None:
        self._cwd = workspace.expanduser().resolve() if workspace is not None else None

    # ---- pure helpers (unit-tested) ---------------------------------------

    @staticmethod
    def _review_diff(diff_text: str) -> dict[str, object]:
        warnings: list[dict[str, str]] = []

        for line in diff_text.splitlines():
            stripped = line[1:] if line[:1] in "+-" else line
            if _SECRET_RE.search(stripped):
                warnings.append({"type": "secret", "line": line})
                break
        for line in diff_text.splitlines():
            stripped = line[1:] if line[:1] in "+-" else line
            if _BREAKPOINT_RE.search(stripped):
                warnings.append({"type": "debug", "line": line})
                break

        changed_lines = [
            line for line in diff_text.splitlines() if line[:1] in "+-"
        ]
        ws_changes = [line for line in changed_lines if _WS_ONLY_RE.match(line)]
        if changed_lines and len(ws_changes) / len(changed_lines) > 0.5:
            warnings.append(
                {
                    "type": "whitespace",
                    "line": (
                        f"{len(ws_changes)} of {len(changed_lines)} changed lines are "
                        "whitespace-only; possible unintended reformatting"
                    ),
                }
            )

        files_changed = len(_FILES_CHANGED_RE.findall(diff_text))
        if files_changed > 20:
            warnings.append(
                {
                    "type": "scope",
                    "line": f"{files_changed} files changed; consider a smaller, focused diff",
                }
            )

        return {"warnings": warnings, "files_changed": files_changed}

    # ---- orchestration ----------------------------------------------------

    @staticmethod
    async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await proc.communicate()

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GitDiffParams.model_validate(params)
        cmd = ["git", "diff"]
        if p.staged:
            cmd.append("--staged")
        if p.path:
            cmd.append("--")
            cmd.append(p.path)
        command = " ".join(cmd)

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self._cwd,
                start_new_session=True,
            )
            stdout_bytes, _ = await proc.communicate()
        except Exception as exc:  # git not installed, not a repo, etc.
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        diff_text = stdout_bytes.decode("utf-8", errors="replace")
        review = self._review_diff(diff_text)

        truncated = len(stdout_bytes) > _MAX_OUTPUT_BYTES
        display = diff_text
        if truncated:
            display = display[-_MAX_OUTPUT_BYTES:]
            display = "[... diff truncated ...]\n" + display

        payload = {
            "diff": display,
            "truncated": truncated,
            "warnings": review["warnings"],
            "files_changed": review["files_changed"],
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return ToolResult(content=content)
