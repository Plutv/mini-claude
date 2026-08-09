from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.loop import AgentLoop
from kama_claude.core.tools.artifacts import ToolArtifactStore
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.registry import ToolRegistry


class DelayTool(BaseTool):
    description = "Benchmark an I/O wait"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, name: str, delay: float, *, parallel_safe: bool) -> None:
        self.name = name
        self.delay = delay
        self.parallel_safe = parallel_safe

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        await asyncio.sleep(self.delay)
        return ToolResult(content=self.name)


async def measure_batch(*, parallel_safe: bool, repeats: int = 7) -> float:
    samples: list[float] = []
    for _ in range(repeats):
        registry = ToolRegistry()
        calls: list[ToolCallBlock] = []
        for index in range(4):
            name = f"io_{index}"
            registry.register(DelayTool(name, 0.05, parallel_safe=parallel_safe))
            calls.append(ToolCallBlock(id=str(index), name=name, input={}))
        loop = AgentLoop(object(), registry, EventBus())  # type: ignore[arg-type]
        started = time.perf_counter()
        await loop._invoke_requested_tools(calls, "benchmark-run")
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


async def measure_artifact() -> tuple[int, int]:
    original = "x" * 100_000
    with tempfile.TemporaryDirectory() as directory:
        store = ToolArtifactStore(Path(directory), threshold=32 * 1024)
        result = await store.externalize(
            ToolCallBlock(id="call-1", name="benchmark", input={}),
            ToolResult(content=original),
        )
        return len(original.encode()), len(result.content.encode())


async def main() -> None:
    serial = await measure_batch(parallel_safe=False)
    parallel = await measure_batch(parallel_safe=True)
    original_bytes, reference_bytes = await measure_artifact()
    print(
        json.dumps(
            {
                "four_io_calls_serial_ms": round(serial * 1_000, 2),
                "four_io_calls_parallel_ms": round(parallel * 1_000, 2),
                "parallel_speedup": round(serial / parallel, 2),
                "artifact_original_bytes": original_bytes,
                "artifact_context_bytes": reference_bytes,
                "artifact_context_reduction_pct": round(
                    (1 - reference_bytes / original_bytes) * 100, 2
                ),
                "artifact_estimated_tokens_before": original_bytes // 4,
                "artifact_estimated_tokens_after": reference_bytes // 4,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
