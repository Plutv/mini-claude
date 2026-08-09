from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from kama_claude.core.agents.loader import AgentProfile, AgentProfileLoader
from kama_claude.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.loop import AgentLoop
from kama_claude.core.runs import new_run_id
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin.bash import BashTool
from kama_claude.core.tools.builtin.list_dir import ListDirTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.task_create import TaskCreateTool
from kama_claude.core.tools.builtin.task_get import TaskGetTool
from kama_claude.core.tools.builtin.task_list import TaskListTool
from kama_claude.core.tools.builtin.task_update import TaskUpdateTool
from kama_claude.core.tools.builtin.write_file import WriteFileTool
from kama_claude.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from kama_claude.core.llm.base import LLMProvider
    from kama_claude.core.permissions.manager import PermissionManager

_profile_loader = AgentProfileLoader()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    run_in_background: bool = False
    subagent_type: str = ""
    timeout_s: float = Field(default=300.0, gt=0, le=3600)
    allowed_tools: list[str] = Field(default_factory=list)


# 在隔离的冷启动上下文中派生子 agent，支持前台阻塞和后台并行两种模式
class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task. "
        "The sub-agent starts with a clean context containing only the provided prompt — "
        "it does not inherit the current conversation history. "
        "Use run_in_background=true to run in parallel; retrieve result later with agent_result."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "3-5 word task description shown in progress display",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete task description including all context the sub-agent needs. "
                    "The sub-agent cannot see the parent conversation, so be explicit."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": "When true, returns immediately with a run_id; use agent_result to poll.",  # noqa: E501
            },
            "subagent_type": {
                "type": "string",
                "description": "Agent role profile (planner/executor/reviewer). Leave empty for default.",  # noqa: E501
            },
            "timeout_s": {
                "type": "number",
                "minimum": 1,
                "maximum": 3600,
                "description": "Maximum child execution time in seconds.",
            },
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造 SpawnAgentTool；depth=0 表示根 agent，最大允许嵌套深度为 2
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        task_registry: BackgroundTaskRegistry,
        runs_dir: Path,
        session_id: str,
        depth: int = 0,
    ) -> None:
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._task_registry = task_registry
        self._runs_dir = runs_dir
        self._session_id = session_id
        self._depth = depth

    # 派生子 agent，前台时阻塞直到完成并返回结果，后台时立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)

        if self._depth >= 2:
            return ToolResult(
                content="Subagent nesting limit (2) reached; cannot spawn further subagents.",
                is_error=True,
                error_type="runtime_error",
            )

        profile: AgentProfile | None = None
        if p.subagent_type:
            profile = _profile_loader.load(p.subagent_type)

        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            system_prompt_override=profile.system_prompt if profile else None,
        )

        child_bus = EventBus()

        # 将子 bus 所有事件桥接到父 bus，TUI 据此渲染嵌套进度
        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish(event)

        child_bus.subscribe(_bridge)

        child_registry = self._build_child_registry(
            child_bus,
            child_run_id,
            profile,
            allowed_tools=p.allowed_tools or None,
        )
        child_loop = AgentLoop(
            self._provider,
            child_registry,
            child_bus,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
        )

        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                description=p.description,
                ts=_now(),
            )
        )

        child_run_path = self._runs_dir / child_run_id
        child_run_path.mkdir(parents=True, exist_ok=True)

        if p.run_in_background:
            try:
                self._task_registry.spawn(
                    run_id=child_run_id,
                    parent_run_id=self._parent_run_id,
                    session_id=self._session_id,
                    description=p.description,
                    depth=self._depth + 1,
                    context=child_context,
                    work=lambda: self._run_background(
                        child_loop,
                        child_context,
                        child_bus,
                        child_run_path,
                        child_run_id,
                    ),
                    timeout_s=p.timeout_s,
                )
            except RuntimeError as exc:
                return ToolResult(
                    content=str(exc),
                    is_error=True,
                    error_type="runtime_error",
                )
            return ToolResult(
                content=(
                    f"Subagent started in background. run_id={child_run_id}. "
                    f"Use agent_result(run_id='{child_run_id}') to retrieve result."
                )
            )

        try:
            async with asyncio.timeout(p.timeout_s):
                async with EventWriter(child_run_path / "events.jsonl") as writer:
                    writer.subscribe(child_bus)
                    await child_loop.run(child_context)
        except TimeoutError:
            if not child_context.is_done():
                child_context.mark_failed("subagent_timeout")
        finally:
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=child_run_id,
                    parent_run_id=self._parent_run_id,
                    status=child_context.status,
                    ts=_now(),
                )
            )

        if child_context.status == "success":
            return ToolResult(
                content=child_context.result or "Subagent completed with no text output."
            )
        return ToolResult(
            content=(
                child_context.result
                or f"Subagent failed (status={child_context.status}, reason={child_context.reason})"
            ),
            is_error=True,
            error_type="runtime_error",
        )

    # 后台任务协程：写事件文件，运行 loop，发布完成事件
    async def _run_background(
        self,
        loop: AgentLoop,
        context: ExecutionContext,
        bus: EventBus,
        run_path: Path,
        run_id: str,
    ) -> None:
        try:
            async with EventWriter(run_path / "events.jsonl") as writer:
                writer.subscribe(bus)
                await loop.run(context)
        except asyncio.CancelledError:
            if not context.is_done():
                context.mark_failed("cancelled")
            raise
        finally:
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=run_id,
                    parent_run_id=self._parent_run_id,
                    status=context.status,
                    ts=_now(),
                )
            )

    # 构造子 registry；基于角色配置过滤工具，深度允许时注册嵌套 SpawnAgentTool
    def _build_child_registry(
        self,
        child_bus: EventBus,
        child_run_id: str,
        profile: AgentProfile | None,
        allowed_tools: list[str] | None = None,
    ) -> ToolRegistry:
        from kama_claude.core.task.manager import TaskManager

        profile_tools = set(profile.allowed_tools) if profile and profile.allowed_tools else None
        requested_tools = set(allowed_tools) if allowed_tools else None
        allowed: set[str] | None
        if profile_tools is not None and requested_tools is not None:
            allowed = profile_tools & requested_tools
        else:
            allowed = profile_tools if profile_tools is not None else requested_tools

        def _allowed(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        _all_tools = [
            ReadFileTool(),
            BashTool(),
            WriteFileTool(),
            ListDirTool(),
        ]
        for t in _all_tools:
            if _allowed(t.name):
                registry.register(t)

        child_task_manager = TaskManager(self._runs_dir / child_run_id / ".tasks")
        for t in [
            TaskCreateTool(child_task_manager),
            TaskUpdateTool(child_task_manager),
            TaskListTool(child_task_manager),
            TaskGetTool(child_task_manager),
        ]:
            if _allowed(t.name):
                registry.register(t)

        if self._depth < 1:
            nested = SpawnAgentTool(
                provider=self._provider,
                parent_bus=child_bus,
                parent_run_id=child_run_id,
                permission_manager=self._permission_manager,
                max_steps=self._max_steps,
                task_registry=self._task_registry,
                runs_dir=self._runs_dir,
                session_id=self._session_id,
                depth=self._depth + 1,
            )
            if _allowed("spawn_agent"):
                registry.register(nested)
            if _allowed("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
            if _allowed("cancel_agent"):
                registry.register(AgentCancelTool(self._task_registry))

        return registry


class AgentResultParams(BaseModel):
    run_id: str
    wait: bool = True
    timeout_s: float = Field(default=30.0, ge=0, le=300)


# 查询后台 subagent 的执行状态和最终结果
class AgentResultTool(BaseTool):
    name = "agent_result"
    description = (
        "Retrieve the result of a background sub-agent previously started with spawn_agent. "
        "Returns 'still running' if the sub-agent has not yet completed."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run_id returned by spawn_agent(run_in_background=true)",
            },
            "wait": {"type": "boolean", "description": "Wait briefly for completion."},
            "timeout_s": {"type": "number", "minimum": 0, "maximum": 300},
        },
        "required": ["run_id"],
    }
    params_model = AgentResultParams

    # 初始化，持有共享的后台任务注册表
    def __init__(self, task_registry: BackgroundTaskRegistry) -> None:
        self._task_registry = task_registry

    # 查询指定 run_id 的后台任务状态，返回结果或错误
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        record = (
            await self._task_registry.wait(p.run_id, p.timeout_s)
            if p.wait
            else self._task_registry.get_record(p.run_id)
        )
        if record is None:
            return ToolResult(
                content=f"Unknown run_id: {p.run_id}.",
                is_error=True,
                error_type="runtime_error",
            )
        if record.status in {"pending", "running"}:
            return ToolResult(content=f"still running status={record.status}")
        if record.status in {"cancelled", "interrupted", "timed_out", "failed"}:
            return ToolResult(
                content=(
                    f"Subagent {record.status}. reason={record.reason or 'unknown'} "
                    f"result={record.result}"
                ),
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=record.result or "Subagent completed with no text result.")


class AgentCancelParams(BaseModel):
    run_id: str


class AgentCancelTool(BaseTool):
    name = "cancel_agent"
    description = "Cancel a running background sub-agent by run_id."
    params_model = AgentCancelParams
    input_schema = {
        "type": "object",
        "properties": {"run_id": {"type": "string"}},
        "required": ["run_id"],
    }

    def __init__(self, task_registry: BackgroundTaskRegistry) -> None:
        self._task_registry = task_registry

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        run_id = AgentCancelParams.model_validate(params).run_id
        if not await self._task_registry.cancel(run_id):
            return ToolResult(
                content=f"Subagent {run_id} is not running.",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=f"Subagent {run_id} cancelled.")
