from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from kama_claude.core.compact.budget import (
    snip_stale_tool_results,
    tool_pairs_balanced,
)
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.engine import ContextEngine, ContextPolicy
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, UsageStats


def _tool_history(count: int = 6, *, size: int = 2_000) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "inspect"}]
    for index in range(count):
        tool_id = f"tool-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "read_file",
                            "input": {"path": f"file-{index}.txt"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": str(index) * size,
                        }
                    ],
                },
            ]
        )
    return messages


def _provider(summary: str = "summary") -> Any:
    provider = MagicMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            text=summary,
            usage=UsageStats(input_tokens=100, output_tokens=20),
        )
    )
    return provider


def test_stale_cleanup_preserves_tool_protocol_pairs() -> None:
    messages = _tool_history()

    reduced, stats = snip_stale_tool_results(messages, keep_recent=2)

    assert stats.changed_results == 4
    assert tool_pairs_balanced(reduced)
    assert [
        block["tool_use_id"]
        for message in reduced
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ] == [f"tool-{index}" for index in range(6)]
    assert messages[2]["content"][0]["content"] == "0" * 2_000


async def test_hot_cache_defers_stale_prefix_rewrite(tmp_path: Path) -> None:
    now = 100.0
    engine = ContextEngine(
        Compactor(EventBus(), tmp_path, "sess-1"),
        ContextPolicy(
            context_window_tokens=100_000,
            snip_threshold=0.10,
            microcompact_threshold=0.99,
            hot_cache_override=0.95,
            full_compact_threshold=0.0,
        ),
        clock=lambda: now,
    )
    context = ExecutionContext(run_id="run-1", goal="inspect", max_steps=10)
    context.messages = _tool_history()
    engine.record_usage(UsageStats(500, 10, context_pct=0.50))
    before = deepcopy(context.messages)

    maintenance = await engine.prepare(context, _provider())

    assert maintenance.snipped_results == 0
    assert context.messages == before


async def test_multi_round_microcompact_keeps_recent_results(tmp_path: Path) -> None:
    engine = ContextEngine(
        Compactor(EventBus(), tmp_path, "sess-1"),
        ContextPolicy(
            context_window_tokens=1_000,
            snip_threshold=0.99,
            microcompact_threshold=0.10,
            full_compact_threshold=0.0,
            microcompact_every_steps=4,
            keep_recent_tool_results=2,
        ),
    )
    context = ExecutionContext(run_id="run-1", goal="inspect", max_steps=10)
    context.messages = _tool_history()
    context.step = 4

    maintenance = await engine.prepare(context, _provider())

    assert maintenance.microcompacted_results == 4
    assert tool_pairs_balanced(context.messages)
    contents = [
        block["content"]
        for message in context.messages
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert all("cleared" in content for content in contents[:4])
    assert contents[-2:] == ["4" * 2_000, "5" * 2_000]


async def test_full_compaction_failure_restores_exact_history(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("summary unavailable"))
    context = ExecutionContext(run_id="run-1", goal="inspect", max_steps=10)
    context.messages = _tool_history()
    original = deepcopy(context.messages)
    engine = ContextEngine(
        Compactor(EventBus(), tmp_path, "sess-1"),
        ContextPolicy(context_window_tokens=100, full_compact_threshold=0.10),
    )

    maintenance = await engine.prepare(context, provider)

    assert maintenance.full_compaction_failed
    assert context.messages == original
    assert tool_pairs_balanced(context.messages)


async def test_full_compaction_adds_safe_continuation_message(tmp_path: Path) -> None:
    context = ExecutionContext(run_id="run-1", goal="inspect", max_steps=10)
    context.messages = _tool_history()
    engine = ContextEngine(
        Compactor(EventBus(), tmp_path, "sess-1"),
        ContextPolicy(context_window_tokens=100, full_compact_threshold=0.10),
    )

    maintenance = await engine.prepare(context, _provider("durable summary"))

    assert maintenance.fully_compacted
    assert [message["role"] for message in context.messages] == [
        "user",
        "assistant",
        "user",
    ]
    assert context.messages[0]["content"] == (
        "[Previous conversation summary]\ndurable summary"
    )
    assert tool_pairs_balanced(context.messages)


def test_tool_artifact_threshold_tightens_as_context_fills() -> None:
    engine = ContextEngine(None, ContextPolicy(tool_result_limit=8_000))
    assert engine.tool_artifact_threshold() == 32 * 1024

    engine.record_usage(UsageStats(100, 10, context_pct=0.55))
    assert engine.tool_artifact_threshold() == 16_000

    engine.record_usage(UsageStats(100, 10, context_pct=0.75))
    assert engine.tool_artifact_threshold() == 8_000
