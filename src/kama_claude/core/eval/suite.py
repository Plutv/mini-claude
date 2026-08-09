from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kama_claude.core.eval.trajectory import TrajectoryMetrics, evaluate_trajectory


@dataclass
class EvalCase:
    name: str
    events_path: Path
    expected_status: str = "success"
    required_tools: list[str] = field(default_factory=list)
    max_steps: int | None = None
    max_input_tokens: int | None = None
    max_tool_failures: int = 0


@dataclass
class EvalResult:
    name: str
    passed: bool
    checks: dict[str, bool]
    metrics: TrajectoryMetrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "checks": self.checks,
            "metrics": asdict(self.metrics),
        }


class EvalSuite:
    def evaluate(self, case: EvalCase) -> EvalResult:
        metrics = evaluate_trajectory(case.events_path)
        checks = {
            "status": metrics.status == case.expected_status,
            "required_tools": set(case.required_tools).issubset(metrics.tools),
            "tool_pairs_complete": not metrics.incomplete_tool_calls,
            "tool_failures": metrics.tool_failures <= case.max_tool_failures,
        }
        if case.max_steps is not None:
            checks["step_budget"] = metrics.steps <= case.max_steps
        if case.max_input_tokens is not None:
            checks["input_token_budget"] = metrics.input_tokens <= case.max_input_tokens
        return EvalResult(
            name=case.name,
            passed=all(checks.values()),
            checks=checks,
            metrics=metrics,
        )

    def run(self, cases: list[EvalCase]) -> list[EvalResult]:
        return [self.evaluate(case) for case in cases]
