from __future__ import annotations

from pydantic import BaseModel, Field

from kama_claude.core.plan.controller import PlanController
from kama_claude.core.tools.base import BaseTool, ToolResult


class EnterPlanParams(BaseModel):
    objective: str = Field(min_length=1)


class EnterPlanModeTool(BaseTool):
    name = "enter_plan_mode"
    description = "Enter durable read-only planning mode before a complex or risky task."
    params_model = EnterPlanParams
    input_schema = {
        "type": "object",
        "properties": {"objective": {"type": "string"}},
        "required": ["objective"],
    }

    def __init__(self, controller: PlanController) -> None:
        self._controller = controller

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        state = self._controller.enter(EnterPlanParams.model_validate(params).objective)
        return ToolResult(content=f"plan mode entered revision={state.revision}")


class UpdatePlanParams(BaseModel):
    plan: str = Field(min_length=1)


class UpdatePlanTool(BaseTool):
    name = "update_plan"
    description = "Persist the proposed implementation plan while remaining read-only."
    params_model = UpdatePlanParams
    input_schema = {
        "type": "object",
        "properties": {"plan": {"type": "string"}},
        "required": ["plan"],
    }

    def __init__(self, controller: PlanController) -> None:
        self._controller = controller

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            state = self._controller.update(UpdatePlanParams.model_validate(params).plan)
        except RuntimeError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(content=f"plan persisted revision={state.revision}")


class RequestExecutionParams(BaseModel):
    summary: str = Field(min_length=1)


class RequestExecutionTool(BaseTool):
    name = "request_execution"
    description = (
        "Request explicit human approval for the durable plan. The runtime prompts the "
        "user before this tool can switch back to execution mode."
    )
    params_model = RequestExecutionParams
    input_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
    }

    def __init__(self, controller: PlanController) -> None:
        self._controller = controller

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        RequestExecutionParams.model_validate(params)
        try:
            state = self._controller.approve_execution()
        except RuntimeError as exc:
            return ToolResult(content=str(exc), is_error=True, error_type="runtime_error")
        return ToolResult(
            content=(
                f"execution approved revision={state.revision}; "
                "continue with the approved plan"
            )
        )
