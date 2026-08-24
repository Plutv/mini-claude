from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.git_diff import GitDiffTool

_SECRET_DIFF = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n+++ b/x.py\n"
    "@@ -1 +1 @@\n"
    "-api_key = 'SUPERSECRET123'\n"
    "+api_key = 'NEWSECRET456'\n"
)

_DEBUG_DIFF = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n+++ b/x.py\n"
    "@@ -1 +1 @@\n"
    "-foo()\n"
    "+import pdb; pdb.set_trace()\n"
)

_CLEAN_DIFF = (
    "diff --git a/x.py b/x.py\n"
    "--- a/x.py\n+++ b/x.py\n"
    "@@ -1 +1 @@\n"
    "-foo\n"
    "+bar\n"
)


def _ws_diff(n: int) -> str:
    parts = []
    for i in range(n):
        parts.append(f"diff --git a/f{i}.py b/f{i}.py\n")
        parts.append("@@ -1 +1 @@\n")
        parts.append("-    \n")
        parts.append("+  \n")
    return "".join(parts)


def test_review_secret() -> None:
    r = GitDiffTool._review_diff(_SECRET_DIFF)
    assert any(w["type"] == "secret" for w in r["warnings"])


def test_review_debug() -> None:
    r = GitDiffTool._review_diff(_DEBUG_DIFF)
    assert any(w["type"] == "debug" for w in r["warnings"])


def test_review_whitespace() -> None:
    r = GitDiffTool._review_diff(_ws_diff(10))
    assert any(w["type"] == "whitespace" for w in r["warnings"])


def test_review_scope_many_files() -> None:
    diff = "".join(f"diff --git a/file{i}.py b/file{i}.py\n" for i in range(25))
    r = GitDiffTool._review_diff(diff)
    assert r["files_changed"] == 25
    assert any(w["type"] == "scope" for w in r["warnings"])


def test_review_clean() -> None:
    r = GitDiffTool._review_diff(_CLEAN_DIFF)
    assert r["warnings"] == []


class _FakeProc:
    returncode = 0

    def __init__(self, out: bytes) -> None:
        self._out = out

    async def communicate(self) -> tuple[bytes, None]:
        return self._out, None


async def test_invoke_reviews_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _fake_create(cmd: str, **kwargs: object) -> _FakeProc:
        captured["cmd"] = cmd
        del kwargs
        return _FakeProc(_CLEAN_DIFF.encode("utf-8"))

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create)
    result = await GitDiffTool(workspace=None).invoke({"staged": True})
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["files_changed"] == 1
    assert payload["warnings"] == []
    assert "--staged" in str(captured["cmd"])


async def test_invoke_git_absent_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(cmd: str, **kwargs: object) -> _FakeProc:
        del cmd, kwargs
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _fake_create)
    result = await GitDiffTool(workspace=None).invoke({})
    assert result.is_error and result.error_type == "runtime_error"
