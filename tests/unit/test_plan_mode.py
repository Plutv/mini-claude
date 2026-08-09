from __future__ import annotations

from pathlib import Path

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import ToolCallBlock
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.permissions.policy import PermissionDecision, evaluate
from kama_claude.core.plan import (
    EnterPlanModeTool,
    PlanController,
    RequestExecutionTool,
    UpdatePlanTool,
)
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.invocation import invoke_tool
from kama_claude.core.tools.registry import ToolRegistry


class _ReadTool(BaseTool):
    name = "read"
    description = "read"
    input_schema = {"type": "object", "properties": {}}
    parallel_safe = True

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="observed")


class _WriteTool(BaseTool):
    name = "write"
    description = "write"
    input_schema = {"type": "object", "properties": {}}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="changed")


def _call(name: str, call_id: str = "tool-1") -> ToolCallBlock:
    return ToolCallBlock(id=call_id, name=name, input={})


async def test_plan_mode_blocks_mutations_but_allows_reads(tmp_path: Path) -> None:
    controller = PlanController(tmp_path / "plan.json")
    controller.enter("inspect before changing")
    registry = ToolRegistry()
    registry.register(_ReadTool())
    registry.register(_WriteTool())
    bus = EventBus()

    read = await invoke_tool(
        registry, _call("read"), bus, "run", plan_controller=controller
    )
    write = await invoke_tool(
        registry, _call("write", "tool-2"), bus, "run", plan_controller=controller
    )

    assert read.content == "observed"
    assert write.is_error is True
    assert write.error_type == "plan_mode_denied"


async def test_plan_is_persisted_and_requires_non_empty_plan(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    controller = PlanController(path)
    enter = EnterPlanModeTool(controller)
    update = UpdatePlanTool(controller)
    request = RequestExecutionTool(controller)

    await enter.invoke({"objective": "fix the bug"})
    rejected = await request.invoke({"summary": "execute"})
    assert rejected.is_error is True

    await update.invoke({"plan": "1. reproduce\n2. patch\n3. test"})
    restored = PlanController(path)
    assert restored.state.mode == "plan"
    assert "reproduce" in restored.state.plan

    approved = await request.invoke({"summary": "execute the saved plan"})
    assert approved.is_error is False
    assert PlanController(path).state.mode == "execute"


async def test_execution_action_budget_is_enforced(tmp_path: Path) -> None:
    controller = PlanController(tmp_path / "plan.json", max_actions=1)
    registry = ToolRegistry()
    registry.register(_WriteTool())
    bus = EventBus()

    first = await invoke_tool(
        registry, _call("write"), bus, "run", plan_controller=controller
    )
    second = await invoke_tool(
        registry, _call("write", "tool-2"), bus, "run", plan_controller=controller
    )

    assert first.content == "changed"
    assert second.is_error is True
    assert second.error_type == "action_budget_exceeded"
    assert PlanController(tmp_path / "plan.json").state.executed_actions == 1


def test_request_execution_always_requires_human_permission() -> None:
    assert evaluate("enter_plan_mode", {}) == PermissionDecision.ALLOW
    assert evaluate("update_plan", {}) == PermissionDecision.ALLOW
    assert evaluate("request_execution", {}) == PermissionDecision.ASK


async def test_plan_approval_ignores_previous_always_allow() -> None:
    manager = PermissionManager(timeout_s=0.02)
    emitted: list[dict[str, object]] = []

    async def emit(raw: dict[str, object]) -> None:
        emitted.append(raw)
        manager.respond(str(raw["tool_use_id"]), "always_allow")

    first = await manager.check_and_wait(
        "approval-1", "request_execution", {"summary": "plan one"}, "session", emit
    )
    second = await manager.check_and_wait(
        "approval-2", "request_execution", {"summary": "plan two"}, "session", emit
    )

    assert first[0] is True
    assert second[0] is True
    assert [item["tool_use_id"] for item in emitted] == ["approval-1", "approval-2"]
