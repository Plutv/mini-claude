from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


class SkillValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    system_prompt_template: str
    allowed_tools: list[str] = field(default_factory=list)
    when_to_use: str = ""
    user_invocable: bool = True
    context: str = "inline"
    source: str = "builtin"
    skill_dir: Path = Path(".")


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text.strip()
    lines = match.group(1).splitlines()
    metadata: dict[str, object] = {}
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            index += 1
            continue
        key, value = stripped.split(":", 1)
        key = key.strip().replace("-", "_")
        value = value.strip()
        if value in {">", "|"}:
            folded = value == ">"
            parts: list[str] = []
            index += 1
            while index < len(lines) and lines[index][:1].isspace():
                parts.append(lines[index].strip())
                index += 1
            metadata[key] = (" ".join(parts) if folded else "\n".join(parts)).strip()
            continue
        if not value:
            items: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip().startswith("- "):
                items.append(_unquote(lines[index].strip()[2:]))
                index += 1
            metadata[key] = items
            continue
        if value.startswith("["):
            try:
                decoded = json.loads(value.replace("'", '"'))
            except json.JSONDecodeError as exc:
                raise SkillValidationError(f"invalid list for {key}") from exc
            metadata[key] = decoded
        elif value.lower() in {"true", "false"}:
            metadata[key] = value.lower() == "true"
        else:
            metadata[key] = _unquote(value)
        index += 1
    return metadata, text[match.end() :].strip()


def _parse_skill_file(path: Path, source: str = "project") -> Skill:
    metadata, body = _parse_frontmatter(path.read_text(encoding="utf-8"))
    name = str(metadata.get("name") or (path.parent.name if path.name == "SKILL.md" else path.stem))
    if not _NAME_RE.fullmatch(name):
        raise SkillValidationError(f"invalid skill name: {name!r}")
    if not body:
        raise SkillValidationError(f"skill {name!r} has an empty prompt")
    context = str(metadata.get("context", "inline"))
    if context not in {"inline", "fork"}:
        raise SkillValidationError("skill context must be 'inline' or 'fork'")
    raw_tools = metadata.get("allowed_tools", [])
    if isinstance(raw_tools, str):
        allowed_tools = [item.strip() for item in raw_tools.split(",") if item.strip()]
    elif isinstance(raw_tools, list):
        allowed_tools = [str(item).strip() for item in raw_tools if str(item).strip()]
    else:
        raise SkillValidationError("allowed_tools must be a list")
    return Skill(
        name=name,
        description=str(metadata.get("description", "")),
        system_prompt_template=body,
        allowed_tools=allowed_tools,
        when_to_use=str(metadata.get("when_to_use", "")),
        user_invocable=bool(metadata.get("user_invocable", True)),
        context=context,
        source=source,
        skill_dir=path.parent.resolve(),
    )


class SkillLoader:
    _BUILTIN_DIR = Path(__file__).parent / "builtin"

    def __init__(
        self,
        *,
        project_dir: Path | None = None,
        user_dir: Path | None = None,
        builtin_dir: Path | None = None,
    ) -> None:
        self._project_dir = project_dir or Path(".kama/skills")
        self._user_dir = user_dir or Path("~/.kama/skills").expanduser()
        self._builtin_dir = builtin_dir or self._BUILTIN_DIR

    def resolve(self, name: str) -> Skill | None:
        if not _NAME_RE.fullmatch(name):
            return None
        for source, path in self._search_paths(name):
            if path.is_file():
                try:
                    return _parse_skill_file(path, source)
                except (OSError, UnicodeError, SkillValidationError):
                    return None
        return None

    def _search_paths(self, name: str) -> list[tuple[str, Path]]:
        paths: list[tuple[str, Path]] = []
        for source, directory in [
            ("project", self._project_dir),
            ("user", self._user_dir),
            ("builtin", self._builtin_dir),
        ]:
            paths.append((source, directory / f"{name}.md"))
            paths.append((source, directory / name / "SKILL.md"))
        return paths

    def list_all(self) -> list[str]:
        return [skill.name for skill in self.list_all_skills()]

    def list_all_skills(self) -> list[Skill]:
        skills: dict[str, Skill] = {}
        for source, directory in [
            ("builtin", self._builtin_dir),
            ("user", self._user_dir),
            ("project", self._project_dir),
        ]:
            if not directory.is_dir():
                continue
            candidates = sorted(directory.glob("*.md")) + sorted(directory.glob("*/SKILL.md"))
            for candidate in candidates:
                try:
                    skill = _parse_skill_file(candidate, source)
                except (OSError, UnicodeError, SkillValidationError):
                    continue
                skills[skill.name] = skill
        return sorted(skills.values(), key=lambda item: item.name)

    def render_prompt(self, skill: Skill, arguments: str) -> str:
        prompt = re.sub(r"\$ARGUMENTS|\$\{ARGUMENTS\}", arguments, skill.system_prompt_template)
        return prompt.replace("${SKILL_DIR}", str(skill.skill_dir)).replace(
            "${CLAUDE_SKILL_DIR}", str(skill.skill_dir)
        )

    def catalog_prompt(self) -> str:
        skills = self.list_all_skills()
        if not skills:
            return ""
        lines = ["## Available Skills"]
        for skill in skills:
            invocation = f"/{skill.name}" if skill.user_invocable else skill.name
            context = f", context={skill.context}"
            lines.append(f"- {invocation}: {skill.description}{context}")
            if skill.when_to_use:
                lines.append(f"  Use when: {skill.when_to_use}")
        lines.append("Use the skill tool for programmatic invocation.")
        return "\n".join(lines)
