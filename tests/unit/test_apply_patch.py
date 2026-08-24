from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.tools.builtin.apply_patch import ApplyPatchTool, _EditPair
from kama_claude.core.tools.file_versions import FileVersionTracker

_SAMPLE_DIFF = (
    "--- a/x.py\n+++ b/x.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def f():\n"
    "-    return 1\n"
    "+    return 2\n"
)


def test_parse_hunks() -> None:
    hunks = ApplyPatchTool._parse_hunks(_SAMPLE_DIFF)
    assert len(hunks) == 1
    old_seq, new_seq = hunks[0]
    assert old_seq == ["def f():", "    return 1"]
    assert new_seq == ["def f():", "    return 2"]


def test_apply_unified_diff_success() -> None:
    old = "def f():\n    return 1\n"
    new, failed = ApplyPatchTool._apply_unified_diff(old, _SAMPLE_DIFF)
    assert failed == []
    assert new == "def f():\n    return 2\n"


def test_apply_unified_diff_failed_hunk() -> None:
    old = "a\nb\nc\n"
    diff = "@@ -1,2 +1,2 @@\n X\n-Y\n+Z\n"
    new, failed = ApplyPatchTool._apply_unified_diff(old, diff)
    assert failed and failed[0]["hunk_index"] == 0
    assert new == old


def test_apply_edits_unique() -> None:
    old = "v = 1\n"
    new, failed = ApplyPatchTool._apply_edits(old, [_EditPair(old="v = 1", new="v = 2")])
    assert failed == [] and new == "v = 2\n"


def test_apply_edits_nonunique() -> None:
    old = "v = 1\nv = 1\n"
    new, failed = ApplyPatchTool._apply_edits(old, [_EditPair(old="v = 1", new="v = 2")])
    assert len(failed) == 1
    assert new == old


async def test_invoke_patch_writes_atomically(tmp_path: Path) -> None:
    target = tmp_path / "x.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = await ApplyPatchTool(workspace=tmp_path).invoke(
        {"path": "x.py", "patch": _SAMPLE_DIFF}
    )
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"
    assert not list(tmp_path.glob(".*.tmp"))


async def test_invoke_edits(tmp_path: Path) -> None:
    target = tmp_path / "x.py"
    target.write_text("v = 1\n", encoding="utf-8")
    result = await ApplyPatchTool(workspace=tmp_path).invoke(
        {"path": "x.py", "edits": [{"old": "v = 1", "new": "v = 2"}]}
    )
    assert not result.is_error
    assert target.read_text(encoding="utf-8") == "v = 2\n"


async def test_invoke_needs_patch_or_edits(tmp_path: Path) -> None:
    result = await ApplyPatchTool(workspace=tmp_path).invoke({"path": "x.py"})
    assert result.is_error and result.error_type == "schema_error"


async def test_invoke_requires_read_first(tmp_path: Path) -> None:
    target = tmp_path / "x.py"
    target.write_text("v = 1\n", encoding="utf-8")
    versions = FileVersionTracker()
    result = await ApplyPatchTool(versions, tmp_path).invoke(
        {"path": "x.py", "edits": [{"old": "v = 1", "new": "v = 2"}]}
    )
    assert result.is_error and result.error_type == "conflict"


async def test_invoke_version_guard_conflict(tmp_path: Path) -> None:
    target = tmp_path / "x.py"
    target.write_text("v = 1\n", encoding="utf-8")
    versions = FileVersionTracker()
    versions.record(target, b"v = 1\n")
    target.write_text("v = 9\n", encoding="utf-8")
    result = await ApplyPatchTool(versions, tmp_path).invoke(
        {"path": "x.py", "edits": [{"old": "v = 1", "new": "v = 2"}]}
    )
    assert result.is_error and result.error_type == "conflict"


async def test_invoke_missing_file(tmp_path: Path) -> None:
    result = await ApplyPatchTool(workspace=tmp_path).invoke(
        {"path": "nope.py", "edits": [{"old": "a", "new": "b"}]}
    )
    assert result.is_error and result.error_type == "schema_error"
