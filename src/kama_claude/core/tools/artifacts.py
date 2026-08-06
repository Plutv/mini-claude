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
_HEAD_CHARS = 2_000
_TAIL_CHARS = 1_000
_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


class ToolArtifactStore:
    """Persist oversized tool output and return a compact model-visible reference."""

    def __init__(self, directory: Path, *, threshold: int = _DEFAULT_THRESHOLD) -> None:
        self._directory = directory
        self._threshold = threshold

    async def externalize(
        self, tool_call: ToolCallBlock, result: ToolResult
    ) -> ToolResult:
        if result.is_error or len(result.content.encode("utf-8")) <= self._threshold:
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
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        preview = result.content[:_HEAD_CHARS]
        if len(result.content) > _HEAD_CHARS + _TAIL_CHARS:
            preview += "\n... [artifact content omitted] ...\n" + result.content[-_TAIL_CHARS:]
        reference = (
            f"{preview}\n\n"
            "[large tool result stored as artifact]\n"
            f"path: {artifact.resolve()}\n"
            f"bytes: {len(encoded)}\n"
            f"sha256: {digest}\n"
            "Use read_file with the artifact path if more detail is required."
        )
        return ToolResult(content=reference)
