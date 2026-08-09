from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

TOOL_RESULT_LIMIT = 8_000
TOOL_RESULT_KEEP = 4_000

_SNIPPABLE_TOOLS = frozenset(
    {
        "bash",
        "grep_search",
        "list_dir",
        "list_files",
        "read_file",
        "run_shell",
        "search",
    }
)


@dataclass(frozen=True)
class ReductionStats:
    changed_results: int = 0
    removed_chars: int = 0


def estimate_message_tokens(messages: list[dict[str, Any]]) -> int:
    """Cheap, provider-independent context estimate used before usage is known."""
    chars = sum(len(str(message.get("content", ""))) for message in messages)
    return max(1, chars // 4)


def _tool_uses(messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    uses: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                uses[str(block.get("id", ""))] = block
    return uses


def _tool_results(
    messages: list[dict[str, Any]],
) -> list[tuple[int, int, dict[str, Any], dict[str, Any] | None]]:
    uses = _tool_uses(messages)
    results: list[tuple[int, int, dict[str, Any], dict[str, Any] | None]] = []
    for message_index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            results.append(
                (
                    message_index,
                    block_index,
                    block,
                    uses.get(str(block.get("tool_use_id", ""))),
                )
            )
    return results


def truncate_tool_results(
    messages: list[dict[str, Any]],
    limit: int = TOOL_RESULT_LIMIT,
    keep: int = TOOL_RESULT_KEEP,
) -> list[dict[str, Any]]:
    """Return a copy with oversized inline tool results reduced head-and-tail."""
    reduced, _ = budget_tool_results(messages, limit=limit, keep=keep)
    return reduced


def budget_tool_results(
    messages: list[dict[str, Any]],
    *,
    limit: int = TOOL_RESULT_LIMIT,
    keep: int = TOOL_RESULT_KEEP,
) -> tuple[list[dict[str, Any]], ReductionStats]:
    result = deepcopy(messages)
    changed = 0
    removed = 0
    head = max(1, keep // 2)
    tail = max(0, keep - head)

    for _, _, block, _ in _tool_results(result):
        text = block.get("content")
        if not isinstance(text, str) or len(text) <= limit:
            continue
        original_len = len(text)
        suffix = text[-tail:] if tail else ""
        omitted = max(0, original_len - head - tail)
        block["content"] = (
            text[:head]
            + f"\n\n[... {omitted} chars omitted from inline context ...]\n\n"
            + suffix
        )
        changed += 1
        removed += original_len - len(str(block["content"]))

    return result, ReductionStats(changed, max(0, removed))


def snip_stale_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_recent: int = 3,
    aggressive: bool = False,
) -> tuple[list[dict[str, Any]], ReductionStats]:
    """Replace old replayable results without deleting their protocol blocks.

    Keeping the ``tool_result`` block and ``tool_use_id`` intact preserves the
    Anthropic tool-use pairing invariant. Error results and side-effect-oriented
    tools are deliberately retained.
    """
    result = deepcopy(messages)
    candidates = []
    for item in _tool_results(result):
        _, _, block, tool_use = item
        if block.get("is_error") or not isinstance(block.get("content"), str):
            continue
        if tool_use is None or str(tool_use.get("name", "")) not in _SNIPPABLE_TOOLS:
            continue
        candidates.append(item)

    if len(candidates) <= keep_recent:
        return result, ReductionStats()

    stale_indices = set(range(max(0, len(candidates) - keep_recent)))
    if not aggressive:
        # A repeated read of the same file makes every older copy stale even
        # when it falls inside the normal recent-result window.
        reads_by_path: dict[str, list[int]] = {}
        for index, (_, _, _, tool_use) in enumerate(candidates):
            assert tool_use is not None
            if tool_use.get("name") != "read_file":
                continue
            raw_input = tool_use.get("input")
            params = raw_input if isinstance(raw_input, dict) else {}
            path = str(params.get("path") or params.get("file_path") or "")
            if path:
                reads_by_path.setdefault(path, []).append(index)
        for indices in reads_by_path.values():
            stale_indices.update(indices[:-1])

    changed = 0
    removed = 0
    for index in sorted(stale_indices):
        _, _, block, tool_use = candidates[index]
        old = str(block["content"])
        tool_name = str(tool_use.get("name", "tool")) if tool_use else "tool"
        marker = (
            "[Old result cleared]"
            if aggressive
            else f"[Previous {tool_name} result removed from active context.]"
        )
        if old == marker:
            continue
        block["content"] = marker
        changed += 1
        removed += max(0, len(old) - len(marker))

    return result, ReductionStats(changed, removed)


def tool_pairs_balanced(messages: list[dict[str, Any]]) -> bool:
    """Return whether every tool_use has exactly one later tool_result."""
    pending: set[str] = set()
    seen_results: set[str] = set()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if message.get("role") == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = str(block.get("id", ""))
                    if not tool_id or tool_id in pending or tool_id in seen_results:
                        return False
                    pending.add(tool_id)
        elif message.get("role") == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_id = str(block.get("tool_use_id", ""))
                    if tool_id not in pending or tool_id in seen_results:
                        return False
                    pending.remove(tool_id)
                    seen_results.add(tool_id)
    return not pending
