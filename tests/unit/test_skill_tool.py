from __future__ import annotations

from pathlib import Path

from kama_claude.core.events.bus import EventBus
from kama_claude.core.skills.loader import SkillLoader
from kama_claude.core.skills.tool import SkillTool


async def test_skill_tool_resolves_prompt_and_publishes_event(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "diagnose"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: diagnose
description: diagnose an incident
user-invocable: false
context: inline
allowed-tools: ["read_file", "bash"]
---
Read ${SKILL_DIR}/runbook.md and diagnose: ${ARGUMENTS}
""",
        encoding="utf-8",
    )
    loader = SkillLoader(
        project_dir=tmp_path / "skills",
        user_dir=tmp_path / "none-user",
        builtin_dir=tmp_path / "none-builtin",
    )
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    tool = SkillTool(loader, bus, "run-1")

    result = await tool.invoke({"skill_name": "diagnose", "arguments": "database timeout"})

    assert not result.is_error
    assert str(skill_dir.resolve()) in result.content
    assert "database timeout" in result.content
    assert "read_file,bash" in result.content
    assert events[0].type == "skill.invoked"  # type: ignore[attr-defined]


async def test_skill_tool_rejects_unknown_skill(tmp_path: Path) -> None:
    loader = SkillLoader(
        project_dir=tmp_path / "project",
        user_dir=tmp_path / "user",
        builtin_dir=tmp_path / "builtin",
    )
    result = await SkillTool(loader, EventBus(), "run-1").invoke(
        {"skill_name": "missing"}
    )

    assert result.is_error
    assert "unknown skill" in result.content
