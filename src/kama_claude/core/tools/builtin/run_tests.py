from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 256 * 1024  # 256 KB
_DEFAULT_TIMEOUT = 120

_PASSED_RE = re.compile(r"(\d+) passed")
_FAILED_RE = re.compile(r"(\d+) failed")
_ERROR_RE = re.compile(r"(\d+) error")
_SKIPPED_RE = re.compile(r"(\d+) skipped")
_FAILED_LINE_RE = re.compile(r"^\s*FAILED\s+(\S+)", re.MULTILINE)


class RunTestsParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    command: str = Field(default="pytest", max_length=2000)
    timeout: int = Field(default=_DEFAULT_TIMEOUT, ge=1, le=300)


class RunTestsTool(BaseTool):
    params_model = RunTestsParams
    name = "run_tests"
    description = (
        "Run a test command (default `pytest`) in the workspace and report a structured "
        "summary: exit code, passed/failed/error/skipped counts, failing test names, and "
        "a tail of the output. Use it to verify edits after applying a patch."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Test command to run (default 'pytest').",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "description": "Maximum seconds to wait (default 120).",
            },
        },
        "required": [],
    }

    def __init__(self, workspace: Path | None = None) -> None:
        self._cwd = workspace.expanduser().resolve() if workspace is not None else None

    # ---- pure helpers (unit-tested) ---------------------------------------

    @staticmethod
    def _parse_pytest_summary(output: str) -> dict[str, object]:
        def _first(pattern: re.Pattern[str]) -> int:
            m = pattern.search(output)
            return int(m.group(1)) if m else 0

        failures = [
            m.group(1) for m in _FAILED_LINE_RE.finditer(output) if m.group(1)
        ]
        return {
            "passed": _first(_PASSED_RE),
            "failed": _first(_FAILED_RE),
            "error": _first(_ERROR_RE),
            "skipped": _first(_SKIPPED_RE),
            "failures": failures,
        }

    # ---- orchestration ----------------------------------------------------

    @staticmethod
    async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is None:
            try:
                if hasattr(os, "killpg"):
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, OSError):
                pass
        await proc.communicate()

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = RunTestsParams.model_validate(params)
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                p.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=self._cwd,
                start_new_session=True,
            )
            try:
                stdout_bytes, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=p.timeout
                )
            except TimeoutError:
                await self._kill_process_group(proc)
                return ToolResult(
                    content=f"[timeout after {p.timeout}s]",
                    is_error=True,
                    error_type="timeout",
                )
            except asyncio.CancelledError:
                await self._kill_process_group(proc)
                raise
        except Exception as exc:  # shell not found, etc.
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")

        output = stdout_bytes.decode("utf-8", errors="replace")
        truncated = len(stdout_bytes) > _MAX_OUTPUT_BYTES
        if truncated:
            output = output[-_MAX_OUTPUT_BYTES:]
            output = "[... output truncated ...]\n" + output

        summary = self._parse_pytest_summary(output)
        duration_ms = int((time.monotonic() - start) * 1000)
        exit_code = proc.returncode or 0
        payload = {
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "passed": summary["passed"],
            "failed": summary["failed"],
            "error": summary["error"],
            "skipped": summary["skipped"],
            "failures": summary["failures"],
            "output_tail": output,
        }
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        return ToolResult(
            content=content,
            is_error=exit_code != 0,
            error_type=None if exit_code == 0 else "runtime_error",
        )
