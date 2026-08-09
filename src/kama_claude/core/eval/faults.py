from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.types import LlmResponse
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.errors import RateLimitedError

FaultKind = Literal["timeout", "connection", "rate_limit", "runtime"]


@dataclass(frozen=True)
class FaultSpec:
    at_call: int
    kind: FaultKind
    latency_ms: int = 0


def _raise_fault(kind: FaultKind) -> None:
    if kind == "timeout":
        raise TimeoutError("injected timeout")
    if kind == "connection":
        raise ConnectionError("injected connection failure")
    if kind == "rate_limit":
        raise RateLimitedError("injected rate limit")
    raise RuntimeError("injected runtime failure")


class FaultInjectingProvider:
    def __init__(self, inner: LLMProvider, faults: list[FaultSpec]) -> None:
        self._inner = inner
        self._faults = {fault.at_call: fault for fault in faults}
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        self.calls += 1
        fault = self._faults.get(self.calls)
        if fault is not None:
            if fault.latency_ms:
                await asyncio.sleep(fault.latency_ms / 1000)
            _raise_fault(fault.kind)
        return await self._inner.chat(
            messages,
            tool_schemas,
            bus,
            run_id,
            step=step,
            system=system,
        )


class FaultInjectingTool(BaseTool):
    def __init__(self, inner: BaseTool, faults: list[FaultSpec]) -> None:
        self._inner = inner
        self._faults = {fault.at_call: fault for fault in faults}
        self.calls = 0
        self.name = inner.name
        self.description = inner.description
        self.input_schema = inner.input_schema
        self.params_model = inner.params_model
        self.parallel_safe = inner.parallel_safe

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        fault = self._faults.get(self.calls)
        if fault is not None:
            if fault.latency_ms:
                await asyncio.sleep(fault.latency_ms / 1000)
            _raise_fault(fault.kind)
        return await self._inner.invoke(params)
