from __future__ import annotations

import json
from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.grep_search import GrepSearchTool


def test_python_grep_finds_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\nneedle_here\ny = 2\n", encoding="utf-8")
    tool = GrepSearchTool(tmp_path)
    matches = tool._python_grep(
        tmp_path, "needle", ignore_case=False, glob=None, max_results=50, context_lines=0
    )
    assert len(matches) == 1
    assert matches[0].path == "a.py"
    assert matches[0].line == 2
    assert matches[0].is_match


def test_python_grep_ignore_case(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")
    tool = GrepSearchTool(tmp_path)
    assert tool._python_grep(
        tmp_path, "needle", ignore_case=True, glob=None, max_results=50, context_lines=0
    )
    assert not tool._python_grep(
        tmp_path, "needle", ignore_case=False, glob=None, max_results=50, context_lines=0
    )


def test_python_grep_context(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("1\n2\nMATCH\n3\n4\n", encoding="utf-8")
    tool = GrepSearchTool(tmp_path)
    matches = tool._python_grep(
        tmp_path, "MATCH", ignore_case=False, glob=None, max_results=50, context_lines=1
    )
    assert len(matches) == 3
    assert len([m for m in matches if m.is_match]) == 1
    assert len([m for m in matches if not m.is_match]) == 2


def test_python_grep_truncates(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text(
        "\n".join(f"hit {i}" for i in range(10)) + "\n", encoding="utf-8"
    )
    tool = GrepSearchTool(tmp_path)
    matches = tool._python_grep(
        tmp_path, "hit", ignore_case=False, glob=None, max_results=3, context_lines=0
    )
    assert len(matches) == 3


def test_python_grep_glob(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hit\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("hit\n", encoding="utf-8")
    tool = GrepSearchTool(tmp_path)
    matches = tool._python_grep(
        tmp_path, "hit", ignore_case=False, glob="*.py", max_results=50, context_lines=0
    )
    assert len(matches) == 1 and matches[0].path == "a.py"


def test_parse_rg_json_line_match() -> None:
    tool = GrepSearchTool()
    line = json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": "src/app.py"},
                "line_number": 42,
                "lines": {"text": "needle here\n"},
            },
        }
    )
    m = tool._parse_rg_json_line(line)
    assert m is not None and m.is_match and m.line == 42 and m.text == "needle here"


def test_parse_rg_json_line_context() -> None:
    tool = GrepSearchTool()
    line = json.dumps(
        {
            "type": "context",
            "data": {
                "path": {"text": "src/app.py"},
                "line_number": 41,
                "lines": {"text": "prev\n"},
            },
        }
    )
    m = tool._parse_rg_json_line(line)
    assert m is not None and not m.is_match and m.line == 41


async def test_invoke_returns_json(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\nneedle\ny = 2\n", encoding="utf-8")
    result = await GrepSearchTool(tmp_path).invoke({"pattern": "needle"})
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["count"] == 1
    assert payload["matches"][0]["path"] == "a.py"


async def test_invoke_invalid_regex(tmp_path: Path) -> None:
    result = await GrepSearchTool(tmp_path).invoke({"pattern": "["})
    assert result.is_error and result.error_type == "schema_error"


async def test_invoke_missing_path(tmp_path: Path) -> None:
    result = await GrepSearchTool(tmp_path).invoke({"pattern": "x", "path": "nope"})
    assert result.is_error and result.error_type == "schema_error"
