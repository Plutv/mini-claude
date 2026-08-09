from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def _duration_ms(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(end) - datetime.fromisoformat(start)
    except ValueError:
        return None
    return max(0, int(delta.total_seconds() * 1000))


@dataclass
class TrajectoryMetrics:
    run_id: str = ""
    status: str = "unknown"
    reason: str | None = None
    steps: int = 0
    duration_ms: int | None = None
    tool_calls: int = 0
    tool_successes: int = 0
    tool_failures: int = 0
    retry_attempts: int = 0
    permission_requests: int = 0
    permission_denials: int = 0
    subagents_started: int = 0
    subagents_finished: int = 0
    context_compactions: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    models: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    incomplete_tool_calls: list[str] = field(default_factory=list)
    malformed_rows: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_trajectory(path: Path) -> TrajectoryMetrics:
    metrics = TrajectoryMetrics()
    started_at: str | None = None
    finished_at: str | None = None
    pending_tools: set[str] = set()
    failed_attempts: dict[str, int] = {}
    if not path.exists():
        raise FileNotFoundError(path)

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            metrics.malformed_rows += 1
            continue
        event_type = event.get("type")
        metrics.run_id = metrics.run_id or str(event.get("run_id", ""))
        if event_type == "run.started":
            started_at = event.get("ts")
        elif event_type == "run.finished":
            metrics.status = str(event.get("status", "unknown"))
            metrics.reason = event.get("reason")
            metrics.steps = int(event.get("steps", 0))
            finished_at = event.get("ts")
        elif event_type == "step.finished":
            metrics.steps = max(metrics.steps, int(event.get("step", 0)))
        elif event_type == "tool.call_started":
            tool_id = str(event.get("tool_use_id", ""))
            pending_tools.add(tool_id)
            metrics.tool_calls += 1
            metrics.tools.append(str(event.get("tool_name", "")))
        elif event_type == "tool.call_finished":
            tool_id = str(event.get("tool_use_id", ""))
            pending_tools.discard(tool_id)
            metrics.retry_attempts += failed_attempts.pop(tool_id, 0)
            metrics.tool_successes += 1
        elif event_type == "tool.call_failed":
            tool_id = str(event.get("tool_use_id", ""))
            failed_attempts[tool_id] = failed_attempts.get(tool_id, 0) + 1
        elif event_type == "permission.requested":
            metrics.permission_requests += 1
        elif event_type == "permission.denied":
            metrics.permission_denials += 1
        elif event_type == "subagent.started":
            metrics.subagents_started += 1
        elif event_type == "subagent.finished":
            metrics.subagents_finished += 1
        elif event_type == "context.compacted":
            metrics.context_compactions += 1
        elif event_type == "llm.model_selected":
            metrics.models.append(str(event.get("model", "")))
        elif event_type == "llm.usage":
            metrics.input_tokens += int(event.get("input_tokens", 0))
            metrics.output_tokens += int(event.get("output_tokens", 0))
            metrics.cache_read_tokens += int(event.get("cache_read_input_tokens", 0))
            metrics.cache_creation_tokens += int(
                event.get("cache_creation_input_tokens", 0)
            )

    for tool_id, attempts in failed_attempts.items():
        metrics.tool_failures += 1
        metrics.retry_attempts += max(0, attempts - 1)
        pending_tools.discard(tool_id)
    metrics.duration_ms = _duration_ms(started_at, finished_at)
    metrics.incomplete_tool_calls = sorted(pending_tools)
    return metrics
