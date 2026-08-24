from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.bus.events import RunFinishedEvent, RunStartedEvent
from kama_claude.core.compact.compactor import Compactor
from kama_claude.core.compact.engine import ContextEngine, ContextPolicy
from kama_claude.core.config import KamaConfig
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus, EventHandler
from kama_claude.core.events.writer import EventWriter
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.factory import build_provider
from kama_claude.core.loop import AgentLoop
from kama_claude.core.mcp.server import McpServerManager
from kama_claude.core.memory import MemoryStore, load_context_file, project_memory_scope
from kama_claude.core.permissions.manager import PermissionManager
from kama_claude.core.plan import (
    EnterPlanModeTool,
    PlanController,
    RequestExecutionTool,
    UpdatePlanTool,
)
from kama_claude.core.runs import RUNS_DIR, new_run_id
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore
from kama_claude.core.skills import Skill, SkillLoader, SkillTool
from kama_claude.core.subagent.registry import BackgroundTaskRegistry
from kama_claude.core.subagent.tool import AgentCancelTool, AgentResultTool, SpawnAgentTool
from kama_claude.core.task.manager import TaskManager
from kama_claude.core.tools.artifacts import ToolArtifactStore
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.builtin import (
    ApplyPatchTool,
    BashTool,
    EditFileTool,
    GitDiffTool,
    GrepSearchTool,
    ListDirTool,
    NoteSaveTool,
    ReadArtifactTool,
    ReadFileTool,
    RunTestsTool,
    SearchTextTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
)
from kama_claude.core.tools.file_versions import FileVersionTracker
from kama_claude.core.tools.registry import ToolRegistry
from kama_claude.core.trace.provider import TracingProvider
from kama_claude.core.trace.writer import TraceWriter


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class CheckpointError(RuntimeError):
    """A balanced context snapshot could not be made durable."""


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: KamaConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        runs_dir: Path | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        memory_store: MemoryStore | None = None,
        task_registry: BackgroundTaskRegistry | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir or RUNS_DIR
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        self._memory_store = memory_store
        self._skill_loader = SkillLoader()
        # 跨 run 共享的后台 subagent 任务注册表
        self._task_registry = task_registry or BackgroundTaskRegistry()

    # 构建工具注册表，注入 TaskManager（任务工具共享同一实例）；可选注入 SpawnAgentTool
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        file_versions: FileVersionTracker | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        child_runs_dir: Path | None = None,
        session_id: str = "",
        tool_whitelist: list[str] | None = None,
        memory_store: MemoryStore | None = None,
        project_scope: str = "",
        plan_controller: PlanController | None = None,
        workspace: Path | None = None,
        artifact_store: ToolArtifactStore | None = None,
    ) -> ToolRegistry:
        allowed: set[str] | None = set(tool_whitelist) if tool_whitelist else None

        def _ok(name: str) -> bool:
            return allowed is None or name in allowed

        registry = ToolRegistry()
        if artifact_store is not None and _ok("read_artifact"):
            registry.register(ReadArtifactTool(artifact_store.directory))
        if plan_controller is not None:
            for plan_tool in (
                EnterPlanModeTool(plan_controller),
                UpdatePlanTool(plan_controller),
                RequestExecutionTool(plan_controller),
            ):
                if _ok(plan_tool.name):
                    registry.register(plan_tool)
        for t in [
            ReadFileTool(file_versions, workspace),
            SearchTextTool(workspace),
            BashTool(workspace),
            WriteFileTool(file_versions, workspace),
            EditFileTool(file_versions, workspace),
            ListDirTool(workspace),
        ]:
            if _ok(t.name):
                registry.register(t)
        for t in [
            GrepSearchTool(workspace),
            ApplyPatchTool(file_versions, workspace),
            RunTestsTool(workspace),
            GitDiffTool(workspace),
        ]:
            if _ok(t.name):
                registry.register(t)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            if _ok(t.name):
                registry.register(t)
        if session is not None and store is not None and run_id is not None:
            note_tool = NoteSaveTool(
                store,
                session.id,
                run_id,
                memory_store=memory_store,
                project_scope=project_scope,
            )
            if _ok(note_tool.name):
                registry.register(note_tool)
        if provider is not None and bus is not None and run_id is not None:
            runs_dir = child_runs_dir or self._runs_dir
            spawn_tool = SpawnAgentTool(
                provider=provider,
                parent_bus=bus,
                parent_run_id=run_id,
                permission_manager=self._permission_manager,
                max_steps=self._config.agent.max_steps,
                task_registry=self._task_registry,
                runs_dir=runs_dir,
                session_id=session_id,
                workspace=workspace,
                depth=0,
            )
            if _ok("spawn_agent"):
                registry.register(spawn_tool)
            if _ok("skill"):
                async def _fork_skill(
                    skill: Skill, prompt: str, arguments: str
                ) -> ToolResult:
                    del arguments
                    return await spawn_tool.invoke(
                        {
                            "description": f"skill:{skill.name}",
                            "prompt": prompt,
                            "run_in_background": False,
                            "allowed_tools": skill.allowed_tools,
                        }
                    )

                registry.register(
                    SkillTool(
                        self._skill_loader,
                        bus,
                        run_id,
                        fork_executor=_fork_skill,
                    )
                )
            if _ok("agent_result"):
                registry.register(AgentResultTool(self._task_registry))
            if _ok("cancel_agent"):
                registry.register(AgentCancelTool(self._task_registry))
        if self._mcp_manager is not None:
            for mcp_tool in self._mcp_manager.get_tools():
                if _ok(mcp_tool.name):
                    registry.register(mcp_tool)
        return registry

    # 执行一次完整的 agent run（委托给 run_and_capture，忽略返回值）
    async def run(self, goal: str, *, run_id: str | None = None) -> None:
        await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        workspace = (
            Path(session.workspace).expanduser().resolve()
            if session is not None and session.workspace
            else Path.cwd().resolve()
        )

        global_ctx = load_context_file(Path("~/.kama/context.md").expanduser())
        project_ctx = load_context_file(workspace / ".kama" / "context.md")
        project_scope = project_memory_scope(workspace)
        memory_store = self._memory_store
        recalled_memories = ""
        if self._config.memory.enabled and memory_store is not None:
            scopes = ["global", project_scope]
            if session is not None:
                scopes.insert(0, f"session:{session.id}")
            recalled = await asyncio.to_thread(
                memory_store.recall,
                goal,
                scopes=scopes,
                limit=self._config.memory.recall_top_k,
                min_score=self._config.memory.recall_min_score,
                max_chars=self._config.memory.recall_max_chars,
            )
            recalled_memories = memory_store.render(recalled)

        task_manager = TaskManager(run_path / ".tasks")
        file_versions = FileVersionTracker()
        artifact_store = ToolArtifactStore(
            run_path / "artifacts",
            threshold=self._config.compaction.tool_result_limit,
            keep_chars=self._config.compaction.tool_result_keep,
        )

        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
            global_context=global_ctx,
            project_context=project_ctx,
            recalled_memories=recalled_memories,
            skill_catalog=self._skill_loader.catalog_prompt(),
            system_prompt_override=system_prompt_override,
        )
        async with EventWriter(run_path / "events.jsonl", run_id=run_id) as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            try:
                provider: LLMProvider = self._provider or build_provider(self._config.llm)
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                session_id_str = session.id if session is not None else ""
                child_runs_dir = (
                    store.runs_dir(session.id)
                    if session is not None and store is not None
                    else self._runs_dir
                )
                session_dir = (
                    store.session_dir(session.id)
                    if session is not None and store is not None
                    else run_path
                )
                plan_controller = PlanController(
                    session_dir / "plan.json",
                    enabled=self._config.plan.enabled,
                    max_actions=self._config.plan.max_actions,
                )
                registry = self._build_registry(
                    task_manager,
                    file_versions=file_versions,
                    session=session,
                    store=store,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    child_runs_dir=child_runs_dir,
                    session_id=session_id_str,
                    tool_whitelist=tool_whitelist,
                    memory_store=memory_store,
                    project_scope=project_scope,
                    plan_controller=plan_controller,
                    workspace=workspace,
                    artifact_store=artifact_store,
                )
                compactor = Compactor(bus, session_dir, session_id_str)
                context_engine = ContextEngine(
                    compactor,
                    ContextPolicy(
                        tool_result_limit=self._config.compaction.tool_result_limit,
                        tool_result_keep=self._config.compaction.tool_result_keep,
                        full_compact_threshold=self._config.compaction.auto_threshold,
                    ),
                )

                checkpoint = None
                if session is not None and store is not None:
                    async def _checkpoint(messages: list[dict[str, Any]]) -> None:
                        try:
                            await asyncio.to_thread(
                                store.write_messages_atomic,
                                session.id,
                                messages,
                            )
                        except Exception as exc:
                            raise CheckpointError(str(exc)) from exc

                    checkpoint = _checkpoint

                loop = AgentLoop(
                    provider, registry, bus,
                    permission_manager=self._permission_manager,
                    context_engine=context_engine,
                    checkpoint=checkpoint,
                    session_id=session_id_str,
                    artifact_store=artifact_store,
                    plan_controller=plan_controller,
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except CheckpointError:
                logging.getLogger(__name__).exception(
                    "session checkpoint failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("persistence_error")
            except Exception:
                logging.getLogger(__name__).exception(
                    "agent run failed run_id=%s step=%d", run_id, context.step
                )
                if not context.is_done():
                    context.mark_failed("llm_error")

            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if cancelled:
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )
