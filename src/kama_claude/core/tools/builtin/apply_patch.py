from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.file_versions import FileVersionTracker
from kama_claude.core.tools.workspace import Workspace

_MAX_BYTES = 1 * 1024 * 1024

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class _EditPair(BaseModel):
    model_config = ConfigDict(extra="ignore")
    old: str = Field(min_length=1)
    new: str = ""


class ApplyPatchParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    patch: str | None = None
    edits: list[_EditPair] | None = None


class ApplyPatchTool(BaseTool):
    params_model = ApplyPatchParams
    name = "apply_patch"
    description = (
        "Apply changes to a file via a unified diff (patch) or a list of old/new "
        "search-replace pairs. The file must have been read first (version guard). "
        "Changes are committed atomically and report which hunks succeeded or failed."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative file path to modify.",
            },
            "patch": {
                "type": "string",
                "description": "Unified diff text (lines starting with @@ / space / + / -).",
            },
            "edits": {
                "type": "array",
                "description": "List of {old, new} exact text replacements (alternative to patch).",
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                    },
                    "required": ["old"],
                },
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

    # ---- pure helpers (unit-tested) ---------------------------------------

    @staticmethod
    def _parse_hunks(diff_text: str) -> list[tuple[list[str], list[str]]]:
        """Return (old_seq, new_seq) line lists for each hunk in a unified diff.

        old_seq = context + removed lines; new_seq = context + added lines.
        """
        hunks: list[tuple[list[str], list[str]]] = []
        old_seq: list[str] = []
        new_seq: list[str] = []
        in_hunk = False
        for raw in diff_text.splitlines():
            if raw.startswith("@@"):
                if in_hunk:
                    hunks.append((old_seq, new_seq))
                    old_seq, new_seq = [], []
                in_hunk = True
                continue
            if not in_hunk:
                continue
            if raw.startswith("+") and not raw.startswith("+++"):
                new_seq.append(raw[1:])
            elif raw.startswith("-") and not raw.startswith("---"):
                old_seq.append(raw[1:])
            elif raw.startswith(" "):
                old_seq.append(raw[1:])
                new_seq.append(raw[1:])
            elif raw.startswith("\\"):
                # "\ No newline at end of file" — ignored for matching
                continue
            else:
                # stray line outside a diff body; ignore
                continue
        if in_hunk:
            hunks.append((old_seq, new_seq))
        return hunks

    @staticmethod
    def _find_subsequence(
        haystack: list[str], needle: list[str], start: int
    ) -> int | None:
        if not needle:
            return start
        n = len(needle)
        for i in range(start, len(haystack) - n + 1):
            if haystack[i : i + n] == needle:
                return i
        return None

    @staticmethod
    def _apply_unified_diff(
        old_text: str, diff_text: str
    ) -> tuple[str, list[dict[str, object]]]:
        # Normalize CRLF so unix-style diffs match on Windows.
        normalized = old_text.replace("\r\n", "\n")
        file_lines = normalized.split("\n")
        failed: list[dict[str, object]] = []
        result = list(file_lines)
        cursor = 0
        for index, (old_seq, new_seq) in enumerate(
            ApplyPatchTool._parse_hunks(diff_text)
        ):
            pos = ApplyPatchTool._find_subsequence(result, old_seq, cursor)
            if pos is None:
                failed.append(
                    {
                        "hunk_index": index,
                        "reason": "context/removed lines not found at expected location",
                    }
                )
                continue
            result[pos : pos + len(old_seq)] = new_seq
            cursor = pos + len(new_seq)
        new_text = "\n".join(result)
        return new_text, failed

    @staticmethod
    def _apply_edits(
        old_text: str, edits: list[_EditPair]
    ) -> tuple[str, list[dict[str, object]]]:
        failed: list[dict[str, object]] = []
        result = old_text.replace("\r\n", "\n")
        for index, pair in enumerate(edits):
            count = result.count(pair.old)
            if count != 1:
                failed.append(
                    {
                        "edit_index": index,
                        "reason": f"expected exactly 1 occurrence, found {count}",
                    }
                )
                continue
            result = result.replace(pair.old, pair.new, 1)
        return result, failed

    # ---- orchestration ----------------------------------------------------

    def _apply(self, params: ApplyPatchParams) -> ToolResult:
        if params.patch is None and params.edits is None:
            return ToolResult(
                content="provide either 'patch' or 'edits'",
                is_error=True,
                error_type="schema_error",
            )

        path = self._workspace.resolve(params.path)
        if not path.exists():
            return ToolResult(
                content=f"file does not exist: {params.path}",
                is_error=True,
                error_type="schema_error",
            )
        if self._version_tracker is not None:
            conflict = self._version_tracker.validate_write(path)
            if conflict is not None:
                return ToolResult(content=conflict, is_error=True, error_type="conflict")

        try:
            original = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(
                content=f"file is not valid UTF-8: {params.path}",
                is_error=True,
                error_type="schema_error",
            )

        if params.patch is not None:
            new_text, failed = self._apply_unified_diff(original, params.patch)
            applied_count = len(self._parse_hunks(params.patch)) - len(failed)
        else:
            assert params.edits is not None
            new_text, failed = self._apply_edits(original, params.edits)
            applied_count = len(params.edits) - len(failed)

        changed_lines = sum(
            1 for a, b in zip(original.split("\n"), new_text.split("\n")) if a != b
        ) + abs(len(new_text.split("\n")) - len(original.split("\n")))

        encoded = new_text.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"patched content too large: {len(encoded)} bytes (limit 1 MB)",
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

        summary = {
            "path": params.path,
            "applied_hunks": applied_count,
            "failed_hunks": failed,
            "changed_lines": changed_lines,
            "sha256": digest,
        }
        ok = len(failed) == 0
        message = (
            f"patched {params.path}: {applied_count} applied, "
            f"{len(failed)} failed hunk(s), {changed_lines} changed line(s), "
            f"sha256={digest}"
        )
        if failed:
            message += "\n" + json.dumps({"failed": failed}, ensure_ascii=False)
        return ToolResult(
            content=message, is_error=not ok, error_type=None if ok else "conflict"
        )

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = ApplyPatchParams.model_validate(params)
        return await asyncio.to_thread(self._apply, parsed)
