from __future__ import annotations

import json
from pathlib import Path

from kama_claude.core.eval import EvalCase, EvalSuite, evaluate_trajectory


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )


def test_trajectory_metrics_cover_retries_tokens_permissions_and_duration(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            {
                "type": "run.started",
                "run_id": "run-1",
                "goal": "inspect",
                "ts": "2026-01-01T00:00:00+00:00",
            },
            {
                "type": "tool.call_started",
                "run_id": "run-1",
                "tool_use_id": "tool-1",
                "tool_name": "read_file",
                "ts": "2026-01-01T00:00:00.100000+00:00",
            },
            {
                "type": "tool.call_failed",
                "run_id": "run-1",
                "tool_use_id": "tool-1",
                "tool_name": "read_file",
                "error_class": "runtime_error",
                "attempt": 1,
                "ts": "2026-01-01T00:00:00.200000+00:00",
            },
            {
                "type": "tool.call_finished",
                "run_id": "run-1",
                "tool_use_id": "tool-1",
                "tool_name": "read_file",
                "ts": "2026-01-01T00:00:00.300000+00:00",
            },
            {
                "type": "permission.requested",
                "run_id": "run-1",
                "tool_use_id": "tool-2",
                "ts": "2026-01-01T00:00:00.400000+00:00",
            },
            {
                "type": "permission.denied",
                "run_id": "run-1",
                "tool_use_id": "tool-2",
                "ts": "2026-01-01T00:00:00.500000+00:00",
            },
            {
                "type": "llm.usage",
                "run_id": "run-1",
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10,
                "ts": "2026-01-01T00:00:00.600000+00:00",
            },
            {
                "type": "run.finished",
                "run_id": "run-1",
                "status": "success",
                "reason": None,
                "steps": 2,
                "ts": "2026-01-01T00:00:01+00:00",
            },
        ],
    )

    metrics = evaluate_trajectory(path)

    assert metrics.status == "success"
    assert metrics.duration_ms == 1000
    assert metrics.tool_successes == 1
    assert metrics.tool_failures == 0
    assert metrics.retry_attempts == 1
    assert metrics.permission_requests == 1
    assert metrics.permission_denials == 1
    assert metrics.input_tokens == 100
    assert metrics.cache_read_tokens == 40
    assert metrics.incomplete_tool_calls == []


def test_eval_suite_fails_missing_tool_and_step_budget(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            {
                "type": "run.started",
                "run_id": "run-1",
                "ts": "2026-01-01T00:00:00+00:00",
            },
            {
                "type": "run.finished",
                "run_id": "run-1",
                "status": "success",
                "steps": 4,
                "ts": "2026-01-01T00:00:01+00:00",
            },
        ],
    )

    result = EvalSuite().evaluate(
        EvalCase(
            "must inspect",
            path,
            required_tools=["read_file"],
            max_steps=3,
        )
    )

    assert result.passed is False
    assert result.checks["required_tools"] is False
    assert result.checks["step_budget"] is False


def test_final_tool_failure_is_not_reported_as_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_events(
        path,
        [
            {
                "type": "tool.call_started",
                "run_id": "run-1",
                "tool_use_id": "tool-1",
                "tool_name": "bash",
            },
            {
                "type": "tool.call_failed",
                "run_id": "run-1",
                "tool_use_id": "tool-1",
                "tool_name": "bash",
                "attempt": 1,
            },
        ],
    )

    metrics = evaluate_trajectory(path)

    assert metrics.tool_failures == 1
    assert metrics.incomplete_tool_calls == []
