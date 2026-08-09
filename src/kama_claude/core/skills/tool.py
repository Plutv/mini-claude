from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from kama_claude.core.bus.events import SkillInvokedEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.tools.base import BaseTool, ToolResult


class SkillInvokeParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    skill_name: str
    arguments: str = ""


class SkillTool(BaseTool):
    name = "skill"
    description = "Invoke a discovered skill and return its resolved instructions."
    params_model = SkillInvokeParams
    parallel_safe = True
    input_schema = {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string"},
            "arguments": {"type": "string"},
        },
        "required": ["skill_name"],
    }

    def __init__(self, loader: SkillLoader, bus: EventBus, run_id: str) -> None:
        self._loader = loader
        self._bus = bus
        self._run_id = run_id

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        parsed = SkillInvokeParams.model_validate(params)
        skill = self._loader.resolve(parsed.skill_name)
        if skill is None:
            return ToolResult(
                content=f"unknown skill: {parsed.skill_name}",
                is_error=True,
                error_type="runtime_error",
            )
        await self._bus.publish(
            SkillInvokedEvent(
                skill_name=skill.name,
                arguments=parsed.arguments,
                run_id=self._run_id,
                ts=datetime.now(UTC).isoformat(),
            )
        )
        prompt = self._loader.render_prompt(skill, parsed.arguments)
        tools = ",".join(skill.allowed_tools) if skill.allowed_tools else "all registered tools"
        return ToolResult(
            content=f"[skill={skill.name} context={skill.context} tools={tools}]\n{prompt}"
        )
