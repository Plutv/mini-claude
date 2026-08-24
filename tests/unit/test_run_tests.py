from __future__ import annotations

import json
import sys

import pytest

from kama_claude.core.tools.builtin.run_tests import RunTestsTool

_SAMPLE = (
    "collected 5 items\n\n"
    "2 passed, 1 failed, 1 error, 1 skipped\n\n"
    "FAILED tests/test_x.py::test_a\n"
    "FAILED tests/test_x.py::test_b\n"
)


def test_parse_pytest_summary_counts() -> None:
    s = RunTestsTool._parse_pytest_summary(_SAMPLE)
    assert s["passed"] == 2
    assert s["failed"] == 1
    assert s["error"] == 1
    assert s["skipped"] == 1
    assert len(s["failures"]) == 2


def test_parse_pytest_summary_no_failures() -> None:
    s = RunTestsTool._parse_pytest_summary("10 passed\n")
    assert s["passed"] == 10
    assert s["failures"] == []


async def test_invoke_runs_command() -> None:
    result = await RunTestsTool().invoke({"command": "echo hello"})
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["exit_code"] == 0
    assert "hello" in payload["output_tail"]


async def test_invoke_nonzero_exit_is_error() -> None:
    result = await RunTestsTool().invoke({"command": "exit 1"})
    assert result.is_error
    payload = json.loads(result.content)
    assert payload["exit_code"] == 1


@pytest.mark.skipif(
    sys.platform != "win32", reason="uses Windows ping for a long-running process"
)
async def test_invoke_timeout() -> None:
    result = await RunTestsTool().invoke(
        {"command": "ping -n 30 127.0.0.1", "timeout": 1}
    )
    assert result.is_error and result.error_type == "timeout"
