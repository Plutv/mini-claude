from __future__ import annotations

import asyncio

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.loop import AgentLoop
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.registry import ToolRegistry


class _ParallelProbeTool(BaseTool):
    description = "Prove that two read-only calls overlap"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}
    parallel_safe = True

    def __init__(self, name: str, state: dict[str, object]) -> None:
        self.name = name
        self._state = state

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self._state["started"] = int(self._state["started"]) + 1
        if self._state["started"] == 2:
            self._state["both_started"].set()
        await asyncio.wait_for(self._state["both_started"].wait(), timeout=0.5)
        return ToolResult(content=self.name)


class _SerialProbeTool(BaseTool):
    description = "Record execution order"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, name: str, order: list[str]) -> None:
        self.name = name
        self._order = order

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self._order.append(f"{self.name}:start")
        await asyncio.sleep(0)
        self._order.append(f"{self.name}:end")
        return ToolResult(content=self.name)


def _loop(registry: ToolRegistry) -> AgentLoop:
    return AgentLoop(object(), registry, EventBus())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_parallel_safe_tools_overlap_but_results_keep_request_order() -> None:
    state: dict[str, object] = {
        "started": 0,
        "both_started": asyncio.Event(),
    }
    registry = ToolRegistry()
    registry.register(_ParallelProbeTool("read_a", state))
    registry.register(_ParallelProbeTool("read_b", state))
    calls = [
        ToolCallBlock(id="2", name="read_b", input={}),
        ToolCallBlock(id="1", name="read_a", input={}),
    ]

    results = await _loop(registry)._invoke_requested_tools(calls, "run-1")

    assert int(state["started"]) == 2
    assert [call.id for call, _ in results] == ["2", "1"]
    assert [result.content for _, result in results] == ["read_b", "read_a"]


@pytest.mark.asyncio
async def test_tools_without_parallel_safe_run_sequentially() -> None:
    order: list[str] = []
    registry = ToolRegistry()
    registry.register(_SerialProbeTool("write_a", order))
    registry.register(_SerialProbeTool("write_b", order))
    calls = [
        ToolCallBlock(id="1", name="write_a", input={}),
        ToolCallBlock(id="2", name="write_b", input={}),
    ]

    await _loop(registry)._invoke_requested_tools(calls, "run-1")

    assert order == ["write_a:start", "write_a:end", "write_b:start", "write_b:end"]
