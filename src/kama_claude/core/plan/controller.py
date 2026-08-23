from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from kama_claude.core.tools.base import BaseTool


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class PlanState:
    mode: str = "execute"
    objective: str = ""
    plan: str = ""
    revision: int = 0
    executed_actions: int = 0
    updated_at: str = ""


class PlanController:
    """Persist plan state and enforce read-only planning plus action budgets."""

    CONTROL_TOOLS = frozenset({"enter_plan_mode", "update_plan", "request_execution"})

    def __init__(self, path: Path, *, enabled: bool = True, max_actions: int = 20) -> None:
        self._path = path
        self._enabled = enabled
        self._max_actions = max_actions
        self.state = self._load()

    def _load(self) -> PlanState:
        if not self._path.exists():
            return PlanState(updated_at=_now())
        try:
            return PlanState(**json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return PlanState(updated_at=_now())

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.parent / (
            f".{self._path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(asdict(self.state), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def enter(self, objective: str) -> PlanState:
        self.state.mode = "plan"
        self.state.objective = objective
        self.state.plan = ""
        self.state.revision += 1
        self.state.executed_actions = 0
        self.state.updated_at = _now()
        self._persist()
        return self.state

    def update(self, plan: str) -> PlanState:
        if self.state.mode != "plan":
            raise RuntimeError("update_plan requires plan mode")
        self.state.plan = plan
        self.state.revision += 1
        self.state.updated_at = _now()
        self._persist()
        return self.state

    def approve_execution(self) -> PlanState:
        if self.state.mode != "plan" or not self.state.plan.strip():
            raise RuntimeError("a non-empty plan is required before execution approval")
        self.state.mode = "execute"
        self.state.updated_at = _now()
        self._persist()
        return self.state

    def guard(self, tool: BaseTool, *, consume_action: bool = False) -> str | None:
        if not self._enabled or tool.name in self.CONTROL_TOOLS:
            return None
        if self.state.mode == "plan" and not tool.read_only:
            return (
                f"tool {tool.name!r} is blocked in plan mode; only read-only tools are "
                "allowed until request_execution is approved"
            )
        if self.state.mode == "execute" and not tool.read_only and consume_action:
            if self.state.executed_actions >= self._max_actions:
                return f"execution action budget exceeded ({self._max_actions})"
            self.state.executed_actions += 1
            self.state.updated_at = _now()
            self._persist()
        return None

    def prompt(self) -> str:
        if not self._enabled:
            return ""
        if self.state.mode == "plan":
            return (
                "\n\n## Plan Mode\n"
                "You are planning. Inspect with read-only tools, update the durable plan, "
                "then call request_execution for human approval. Do not attempt mutations.\n"
                f"Objective: {self.state.objective or '(not set)'}\n"
                f"Current plan:\n{self.state.plan or '(not written yet)'}"
            )
        if self.state.plan:
            remaining = max(0, self._max_actions - self.state.executed_actions)
            return (
                "\n\n## Approved Execution Plan\n"
                f"{self.state.plan}\nRemaining side-effect action budget: {remaining}"
            )
        return ""
