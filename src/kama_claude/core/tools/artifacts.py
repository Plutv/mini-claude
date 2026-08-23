from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path

from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.tools.base import ToolResult

_DEFAULT_THRESHOLD = 32 * 1024
_DEFAULT_KEEP_CHARS = 4_000
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


class ToolArtifactStore:
    """Persist oversized tool output and return a compact model-visible reference."""

    def __init__(
        self,
        directory: Path,
        *,
        threshold: int = _DEFAULT_THRESHOLD,
        keep_chars: int = _DEFAULT_KEEP_CHARS,
    ) -> None:
        self._directory = directory
        self._threshold = threshold
        self._keep_chars = keep_chars

    @property
    def directory(self) -> Path:
        return self._directory

    async def externalize(
        self,
        tool_call: ToolCallBlock,
        result: ToolResult,
        *,
        threshold: int | None = None,
    ) -> ToolResult:
        effective_threshold = self._threshold if threshold is None else threshold
        if len(result.content.encode("utf-8")) <= effective_threshold:
            return result
        return await asyncio.to_thread(self._write, tool_call, result)

    def _write(self, tool_call: ToolCallBlock, result: ToolResult) -> ToolResult:
        self._directory.mkdir(parents=True, exist_ok=True)
        safe_tool = _SAFE_NAME.sub("_", tool_call.name)
        safe_id = _SAFE_NAME.sub("_", tool_call.id)
        artifact = self._directory / f"{safe_tool}-{safe_id}.txt"
        metadata = artifact.with_suffix(".json")
        encoded = result.content.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()

        tmp = artifact.with_suffix(f".tmp-{os.getpid()}")
        tmp.write_bytes(encoded)
        os.replace(tmp, artifact)
        metadata.write_text(
            json.dumps(
                {
                    "tool_name": tool_call.name,
                    "tool_use_id": tool_call.id,
                    "bytes": len(encoded),
                    "sha256": digest,
                    "is_error": result.is_error,
                    "error_type": result.error_type,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        head_chars = max(1, self._keep_chars // 2)
        tail_chars = max(0, self._keep_chars - head_chars)
        preview = result.content[:head_chars]
        if len(result.content) > head_chars + tail_chars:
            suffix = result.content[-tail_chars:] if tail_chars else ""
            preview += "\n... [artifact content omitted] ...\n" + suffix
        result_kind = "error" if result.is_error else "result"
        reference = (
            f"{preview}\n\n"
            f"[large tool {result_kind} stored as artifact]\n"
            f"path: {artifact.resolve()}\n"
            f"bytes: {len(encoded)}\n"
            f"sha256: {digest}\n"
            "Use read_artifact with this path and query text (preferred), or a bounded "
            "offset range, if more detail is required."
        )
        return ToolResult(
            content=reference,
            is_error=result.is_error,
            error_type=result.error_type,
        )
