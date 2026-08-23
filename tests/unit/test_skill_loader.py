from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.skills.loader import (
    Skill,
    SkillLoader,
    SkillValidationError,
    _parse_skill_file,
)


# 功能：内建 review skill 应能被 SkillLoader 查找到
# 设计：直接调用 resolve("review")，不依赖文件系统之外的任何状态
def test_builtin_skill_found() -> None:
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.name == "review"
    assert "审查" in skill.description or "review" in skill.description.lower()
    assert skill.system_prompt_template != ""


# 功能：内建 init / summarize / orchestrate skill 均可找到
# 设计：列举所有内建 skill 名，断言均能解析
@pytest.mark.parametrize("name", ["init", "review", "summarize", "orchestrate"])
def test_all_builtin_skills_found(name: str) -> None:
    loader = SkillLoader()
    skill = loader.resolve(name)
    assert skill is not None, f"builtin skill '{name}' not found"


# 功能：不存在的 skill 名应返回 None
# 设计：查找一个不存在的名称，断言 resolve 返回 None 而非抛异常
def test_unknown_skill_returns_none() -> None:
    loader = SkillLoader()
    result = loader.resolve("nonexistent_skill_xyz")
    assert result is None


# 功能：render_prompt 应将 $ARGUMENTS 替换为传入的参数字符串
# 设计：构造含 $ARGUMENTS 的 skill，验证 render_prompt 结果不含 "$ARGUMENTS" 且含参数值
def test_arguments_substituted() -> None:
    loader = SkillLoader()
    skill = Skill(
        name="test",
        description="test skill",
        system_prompt_template="Review this: $ARGUMENTS\nPlease be thorough.",
        allowed_tools=[],
    )
    rendered = loader.render_prompt(skill, "src/foo.py")
    assert "$ARGUMENTS" not in rendered
    assert "src/foo.py" in rendered


# 功能：frontmatter 中的 allowed_tools 列表应被正确解析
# 设计：构造含 allowed_tools 的 Markdown 文件，通过 _parse_skill_file 解析并验证结果
def test_frontmatter_parsed(tmp_path: Path) -> None:
    content = """\
---
name: custom
description: 自定义 skill 测试
allowed_tools:
  - read_file
  - bash
---
你是一个测试助手，目标：$ARGUMENTS
"""
    p = tmp_path / "custom.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "custom"
    assert skill.description == "自定义 skill 测试"
    assert "read_file" in skill.allowed_tools
    assert "bash" in skill.allowed_tools
    assert "$ARGUMENTS" in skill.system_prompt_template


# 功能：无 frontmatter 的 Markdown 文件仍可加载，allowed_tools 为空列表
# 设计：写入纯正文 Markdown，断言解析成功且 allowed_tools=[]
def test_no_frontmatter(tmp_path: Path) -> None:
    content = "你是助手，请帮助用户完成任务：$ARGUMENTS\n"
    p = tmp_path / "plain.md"
    p.write_text(content, encoding="utf-8")
    skill = _parse_skill_file(p)
    assert skill.name == "plain"
    assert skill.allowed_tools == []
    assert "你是助手" in skill.system_prompt_template


# 功能：项目本地 skill 应覆盖内建同名 skill
# 设计：在 .kama/skills/ 中写入同名文件，用 monkeypatch 修改 cwd，断言加载到的是本地版本
def test_project_overrides_global(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    local_skills = tmp_path / ".kama" / "skills"
    local_skills.mkdir(parents=True)
    (local_skills / "review.md").write_text(
        "---\nname: review\ndescription: local override\n---\nlocal system prompt $ARGUMENTS\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    loader = SkillLoader()
    skill = loader.resolve("review")
    assert skill is not None
    assert skill.description == "local override"
    assert "local system prompt" in skill.system_prompt_template


def test_v2_metadata_and_skill_directory_variables(tmp_path: Path) -> None:
    skill_dir = tmp_path / "deploy"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
name: deploy
description: deploy safely
when-to-use: when a release is approved
user-invocable: false
context: fork
allowed-tools: ["read_file", "bash"]
---
Use ${SKILL_DIR}/checklist.md for $ARGUMENTS
""",
        encoding="utf-8",
    )

    skill = _parse_skill_file(skill_file)
    rendered = SkillLoader().render_prompt(skill, "release 42")

    assert skill.when_to_use == "when a release is approved"
    assert skill.user_invocable is False
    assert skill.context == "fork"
    assert skill.allowed_tools == ["read_file", "bash"]
    assert str(skill_dir.resolve()) in rendered
    assert "release 42" in rendered


def test_invalid_skill_metadata_is_rejected(tmp_path: Path) -> None:
    skill_file = tmp_path / "bad.md"
    skill_file.write_text(
        "---\nname: ../escape\ncontext: invalid\n---\nprompt\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillValidationError):
        _parse_skill_file(skill_file)


def test_project_debug_fix_skill_is_parameterized_and_tool_scoped() -> None:
    loader = SkillLoader(project_dir=Path(".kama/skills"))

    skill = loader.resolve("debug-fix")

    assert skill is not None
    assert skill.source == "project"
    assert skill.context == "inline"
    assert skill.allowed_tools == [
        "search_text",
        "read_file",
        "read_artifact",
        "list_dir",
        "bash",
        "edit_file",
        "write_file",
    ]
    rendered = loader.render_prompt(skill, "pytest tests/unit/test_example.py -q")
    assert "$ARGUMENTS" not in rendered
    assert "pytest tests/unit/test_example.py -q" in rendered
    assert str(skill.skill_dir) in rendered


def test_catalog_includes_when_to_use_and_invocation_mode(tmp_path: Path) -> None:
    project = tmp_path / "skills"
    project.mkdir()
    (project / "auto.md").write_text(
        """---
name: auto
description: automatic diagnosis
when_to_use: when logs contain errors
user_invocable: false
---
diagnose
""",
        encoding="utf-8",
    )
    loader = SkillLoader(
        project_dir=project,
        user_dir=tmp_path / "user",
        builtin_dir=tmp_path / "builtin",
    )

    catalog = loader.catalog_prompt()

    assert "auto: automatic diagnosis" in catalog
    assert "Use when: when logs contain errors" in catalog
    assert "/auto" not in catalog
