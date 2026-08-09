from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.eval import (
    FaultInjectingProvider,
    FaultInjectingTool,
    FaultSpec,
    evaluate_trajectory,
)
from kama_claude.core.events.bus import EventBus
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.errors import RateLimitedError
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


class _Provider:
    async def chat(self, *args: Any, **kwargs: Any) -> LlmResponse:
        return LlmResponse("end_turn", text="ok", usage=UsageStats(1, 1))


class _Tool(BaseTool):
    name = "demo"
    description = "demo"
    input_schema = {"type": "object", "properties": {}}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="ok")


async def test_provider_fault_is_injected_on_exact_call_only() -> None:
    provider = FaultInjectingProvider(
        _Provider(), [FaultSpec(at_call=2, kind="connection")]
    )
    args = ([], [], EventBus(), "run")

    assert (await provider.chat(*args)).text == "ok"
    with pytest.raises(ConnectionError, match="injected"):
        await provider.chat(*args)
    assert (await provider.chat(*args)).text == "ok"


async def test_tool_fault_supports_rate_limit_scenarios() -> None:
    tool = FaultInjectingTool(_Tool(), [FaultSpec(at_call=1, kind="rate_limit")])

    with pytest.raises(RateLimitedError, match="injected"):
        await tool.invoke({})
    assert (await tool.invoke({})).content == "ok"


async def test_injected_rate_limit_is_retried_and_visible_in_trajectory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kama_claude.core.tools.invocation._RETRY_BASE_S", 0)
    registry = ToolRegistry()
    registry.register(
        FaultInjectingTool(_Tool(), [FaultSpec(at_call=1, kind="rate_limit")])
    )
    bus = EventBus()
    events_path = tmp_path / "events.jsonl"

    async with EventWriter(events_path) as writer:
        writer.subscribe(bus)
        result = await invoke_tool(
            registry,
            ToolCallBlock(id="tool-1", name="demo", input={}),
            bus,
            "run-1",
        )

    metrics = evaluate_trajectory(events_path)

    assert result.content == "ok"
    assert metrics.tool_successes == 1
    assert metrics.tool_failures == 0
    assert metrics.retry_attempts == 1
