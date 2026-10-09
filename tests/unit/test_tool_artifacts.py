from __future__ import annotations

import json

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.tools.artifacts import ToolArtifactStore
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


class _LargeOutputTool(BaseTool):
    name = "large_output"
    description = "Return a large deterministic payload"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content=self._content)


class _LargeErrorTool(BaseTool):
    name = "large_error"
    description = "Return a large deterministic error"
    input_schema: dict[str, object] = {"type": "object", "properties": {}}

    def __init__(self, content: str) -> None:
        self._content = content

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(
            content=self._content,
            is_error=True,
            error_type="schema_error",
        )


@pytest.mark.asyncio
async def test_large_tool_output_is_persisted_and_replaced_by_reference(tmp_path) -> None:
    original = "header\n" + ("x" * 10_000) + "\nfooter"
    registry = ToolRegistry()
    registry.register(_LargeOutputTool(original))
    store = ToolArtifactStore(tmp_path / "artifacts", threshold=1_024)
    call = ToolCallBlock(id="call/1", name="large_output", input={})
    events = []
    bus = EventBus()

    async def collect(event) -> None:
        events.append(event)

    bus.subscribe(collect)

    result = await invoke_tool(
        registry, call, bus, "run-1", artifact_store=store
    )

    artifacts = list((tmp_path / "artifacts").glob("*.txt"))
    assert len(artifacts) == 1
    assert artifacts[0].read_text(encoding="utf-8") == original
    assert len(result.content) < len(original)
    assert "[large tool result stored as artifact]" in result.content
    assert "Use read_artifact" in result.content
    assert str(artifacts[0].resolve()) in result.content
    metadata = json.loads(artifacts[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["bytes"] == len(original.encode("utf-8"))
    finished = next(event for event in events if event.type == "tool.call_finished")
    assert finished.output == result.content


@pytest.mark.asyncio
async def test_small_tool_output_stays_inline(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_LargeOutputTool("small"))
    store = ToolArtifactStore(tmp_path / "artifacts", threshold=1_024)
    call = ToolCallBlock(id="call-2", name="large_output", input={})

    result = await invoke_tool(
        registry, call, EventBus(), "run-1", artifact_store=store
    )

    assert result.content == "small"
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.asyncio
async def test_large_tool_error_is_externalized_without_losing_error_semantics(
    tmp_path,
) -> None:
    original = "Validation failed\n" + ("stack frame\n" * 2_000) + "root cause"
    registry = ToolRegistry()
    registry.register(_LargeErrorTool(original))
    store = ToolArtifactStore(tmp_path / "artifacts", threshold=1_024)
    call = ToolCallBlock(id="error/1", name="large_error", input={})
    events = []
    bus = EventBus()

    async def collect(event) -> None:
        events.append(event)

    bus.subscribe(collect)
    result = await invoke_tool(
        registry,
        call,
        bus,
        "run-error",
        artifact_store=store,
    )

    artifact = tmp_path / "artifacts" / "large_error-error_1.txt"
    assert artifact.read_text(encoding="utf-8") == original
    assert result.is_error is True
    assert result.error_type == "schema_error"
    assert "[large tool error stored as artifact]" in result.content
    assert str(artifact.resolve()) in result.content

    metadata = json.loads(artifact.with_suffix(".json").read_text(encoding="utf-8"))
    assert metadata["is_error"] is True
    assert metadata["error_type"] == "schema_error"
    failed = next(event for event in events if event.type == "tool.call_failed")
    assert failed.error_message == result.content


@pytest.mark.asyncio
async def test_hundred_thousand_character_output_reduces_by_over_ninety_percent(
    tmp_path,
) -> None:
    original = "a" * 100_000
    registry = ToolRegistry()
    registry.register(_LargeOutputTool(original))
    store = ToolArtifactStore(
        tmp_path / "artifacts",
        threshold=8_000,
        keep_chars=4_000,
    )

    result = await invoke_tool(
        registry,
        ToolCallBlock(id="call-100k", name="large_output", input={}),
        EventBus(),
        "run-100k",
        artifact_store=store,
        artifact_threshold=8_000,
    )

    assert len(result.content) < len(original) * 0.10
    assert (tmp_path / "artifacts" / "large_output-call-100k.txt").stat().st_size == 100_000


@pytest.mark.asyncio
async def test_read_file_can_return_a_bounded_region_without_artifact_round_trips(
    tmp_path,
) -> None:
    lines = [f"line-{number:04d} " + "x" * 45 for number in range(1, 1_401)]
    lines[1_116] = "self.format = getattr(schema.opts, self.SCHEMA_OPTS_VAR_NAME)"
    (tmp_path / "fields.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=tmp_path))
    result = await invoke_tool(
        registry,
        ToolCallBlock(
            id="read-region",
            name="read_file",
            input={"path": "fields.py", "start_line": 1_110, "num_lines": 20},
        ),
        EventBus(),
        "run-1",
        artifact_store=ToolArtifactStore(tmp_path / "artifacts"),
    )

    assert "schema.opts" in result.content
    assert "line-0001" not in result.content
    assert "[large tool result stored as artifact]" not in result.content
    assert not (tmp_path / "artifacts").exists()
