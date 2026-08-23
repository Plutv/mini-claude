from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kama_claude.core.compact.budget import tool_pairs_balanced
from kama_claude.core.compact.engine import ContextEngine, ContextPolicy
from kama_claude.core.config import get_config
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.factory import build_provider
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.loop import AgentLoop
from kama_claude.core.tools.artifacts import ToolArtifactStore
from kama_claude.core.tools.base import ToolResult
from kama_claude.core.tools.builtin.read_artifact import ReadArtifactTool
from kama_claude.core.tools.registry import ToolRegistry

DEFAULT_DATASET = Path(__file__).parent / "datasets" / "context_quality_v1.json"


@dataclass
class RequestMetric:
    active_chars: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


@dataclass
class ConditionResult:
    answer: str
    exact_match: bool
    contains_expected: bool
    status: str
    reason: str | None
    model_calls: int
    tool_calls: int
    first_request_chars: int
    total_prompt_tokens: int
    total_output_tokens: int
    requests: list[RequestMetric] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    kind: str
    repetition: int
    expected: str
    baseline: ConditionResult
    managed: ConditionResult
    managed_expected_visible: bool
    managed_expected_in_artifact: bool
    managed_recoverable_offline: bool
    managed_tool_pairs_balanced: bool


class TrackingProvider:
    def __init__(self, delegate: LLMProvider) -> None:
        self._delegate = delegate
        self.requests: list[RequestMetric] = []

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        response = await self._delegate.chat(
            messages,
            tool_schemas,
            bus,
            run_id,
            step=step,
            system=system,
        )
        usage = response.usage or UsageStats(0, 0)
        self.requests.append(
            RequestMetric(
                active_chars=len(
                    json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
                ),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_tokens=usage.cache_read_input_tokens,
                cache_creation_tokens=usage.cache_creation_input_tokens,
            )
        )
        return response


class _UnusedProvider:
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        del messages, tool_schemas, bus, run_id, step, system
        raise AssertionError("offline context preparation must not call an LLM")


def _filler(chars: int) -> str:
    lines: list[str] = []
    size = 0
    index = 0
    while size < chars:
        line = (
            f"log-{index:05d} module=worker-{index % 17} status=ok "
            f"checksum={(index * 2654435761) & 0xFFFFFFFF:08x}\n"
        )
        lines.append(line)
        size += len(line)
        index += 1
    return "".join(lines)[:chars]


def _insert_fact(payload: str, fact: str, placement: str) -> str:
    marker = f"\nRECOVERY_CODE={fact}\n"
    if placement == "head":
        at = 800
    elif placement == "tail":
        at = max(0, len(payload) - 800)
    else:
        at = len(payload) // 2
    return payload[:at] + marker + payload[at:]


def _tool_pair(
    tool_id: str,
    content: str,
    *,
    is_error: bool = False,
    path: str = "build.log",
) -> list[dict[str, Any]]:
    result: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": content,
    }
    if is_error:
        result["is_error"] = True
    return [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "read_file",
                    "input": {"path": path},
                }
            ],
        },
        {"role": "user", "content": [result]},
    ]


def _question(*, artifact_available: bool = False) -> str:
    question = "Return only the exact value after RECOVERY_CODE=. Do not explain."
    if artifact_available:
        question += (
            " If it is omitted from the artifact preview, call read_artifact with "
            "query='RECOVERY_CODE' before answering."
        )
    return question


async def _build_messages(
    case: dict[str, Any],
    *,
    managed: bool,
    artifact_dir: Path,
) -> list[dict[str, Any]]:
    kind = str(case["kind"])
    expected = str(case["expected"])
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "Inspect the supplied diagnostic output."}
    ]

    if kind in {"large_tool_result", "large_tool_error"}:
        payload = _insert_fact(
            _filler(int(case["payload_chars"])), expected, str(case["placement"])
        )
        is_error = kind == "large_tool_error"
        result = ToolResult(
            content=payload,
            is_error=is_error,
            error_type="runtime_error" if is_error else None,
        )
        call = ToolCallBlock(id=f"call-{case['id']}", name="read_file", input={})
        if managed:
            result = await ToolArtifactStore(
                artifact_dir, threshold=8_000, keep_chars=4_000
            ).externalize(call, result)
        messages.extend(_tool_pair(call.id, result.content, is_error=result.is_error))
        messages.append(
            {
                "role": "user",
                "content": _question(artifact_available=managed),
            }
        )
        return messages

    count = int(case["tool_results"])
    result_chars = int(case["result_chars"])
    for index in range(count):
        payload = _filler(result_chars)
        if kind == "stale_duplicate_results":
            value = expected if index == count - 1 else f"KC-CONFIG-V{index}"
            payload = payload[:-100] + f"\nRECOVERY_CODE={value}\n" + payload[-70:]
        messages.extend(
            _tool_pair(
                f"read-{index}",
                payload,
                path="config/runtime.toml",
            )
        )
    if kind == "current_request":
        messages.append(
            {
                "role": "user",
                "content": "Compute 17 * 19 and return only the integer result.",
            }
        )
    else:
        messages.append({"role": "user", "content": _question()})

    if managed:
        context = ExecutionContext(
            run_id=f"prepare-{case['id']}",
            goal="evaluate context",
            max_steps=4,
            prefill_messages=messages,
        )
        context.step = 4
        engine = ContextEngine(
            None,
            ContextPolicy(
                full_compact_threshold=0.0,
                keep_recent_tool_results=3,
                microcompact_every_steps=4,
            ),
            clock=lambda: 100.0,
        )
        engine.record_usage(UsageStats(150_000, 0, context_pct=0.75))
        await engine.prepare(context, _UnusedProvider())
        messages = context.messages
    return messages


def _artifact_contains(artifact_dir: Path, expected: str) -> bool:
    return any(
        expected in path.read_text(encoding="utf-8", errors="replace")
        for path in artifact_dir.glob("*.txt")
    )


def _recoverable(
    messages: list[dict[str, Any]],
    artifact_dir: Path,
    expected: str,
    *,
    kind: str,
) -> bool:
    visible = json.dumps(messages, ensure_ascii=False)
    if kind == "current_request":
        return "Compute 17 * 19" in visible
    return expected in visible or _artifact_contains(artifact_dir, expected)


async def _run_condition(
    provider: LLMProvider,
    messages: list[dict[str, Any]],
    expected: str,
    *,
    artifact_dir: Path | None,
    run_id: str,
) -> ConditionResult:
    tracked = TrackingProvider(provider)
    registry = ToolRegistry()
    if artifact_dir is not None:
        registry.register(ReadArtifactTool(artifact_dir))
    tool_calls = 0
    bus = EventBus()

    async def count_tools(event: Any) -> None:
        nonlocal tool_calls
        if getattr(event, "type", "") == "tool.call_started":
            tool_calls += 1

    bus.subscribe(count_tools)
    context = ExecutionContext(
        run_id=run_id,
        goal="evaluate context answer quality",
        max_steps=4,
        prefill_messages=messages,
    )
    await AgentLoop(tracked, registry, bus).run(context)
    normalized = context.result.strip().strip("`\"'").strip()
    requests = tracked.requests
    return ConditionResult(
        answer=context.result,
        exact_match=normalized.casefold() == expected.casefold(),
        contains_expected=expected.casefold() in context.result.casefold(),
        status=context.status,
        reason=context.reason,
        model_calls=len(requests),
        tool_calls=tool_calls,
        first_request_chars=requests[0].active_chars if requests else 0,
        total_prompt_tokens=sum(item.prompt_tokens for item in requests),
        total_output_tokens=sum(item.output_tokens for item in requests),
        requests=requests,
    )


async def run_live(
    dataset: list[dict[str, Any]],
    *,
    repetitions: int = 1,
) -> dict[str, Any]:
    config = get_config()
    provider = build_provider(config.llm)
    results: list[CaseResult] = []
    try:
        with tempfile.TemporaryDirectory(prefix="kc-context-ab-") as temp:
            root = Path(temp)
            for repetition in range(1, repetitions + 1):
                for case in dataset:
                    case_id = str(case["id"])
                    expected = str(case["expected"])
                    baseline_dir = root / f"repeat-{repetition}" / case_id / "baseline"
                    managed_dir = root / f"repeat-{repetition}" / case_id / "managed"
                    baseline_messages = await _build_messages(
                        case,
                        managed=False,
                        artifact_dir=baseline_dir,
                    )
                    managed_messages = await _build_messages(
                        case,
                        managed=True,
                        artifact_dir=managed_dir,
                    )
                    baseline = await _run_condition(
                        provider,
                        baseline_messages,
                        expected,
                        artifact_dir=None,
                        run_id=f"context-ab-{case_id}-r{repetition}-baseline",
                    )
                    managed = await _run_condition(
                        provider,
                        managed_messages,
                        expected,
                        artifact_dir=managed_dir,
                        run_id=f"context-ab-{case_id}-r{repetition}-managed",
                    )
                    visible = expected in json.dumps(
                        managed_messages, ensure_ascii=False
                    )
                    in_artifact = _artifact_contains(managed_dir, expected)
                    results.append(
                        CaseResult(
                            case_id=case_id,
                            kind=str(case["kind"]),
                            repetition=repetition,
                            expected=expected,
                            baseline=baseline,
                            managed=managed,
                            managed_expected_visible=visible,
                            managed_expected_in_artifact=in_artifact,
                            managed_recoverable_offline=_recoverable(
                                managed_messages,
                                managed_dir,
                                expected,
                                kind=str(case["kind"]),
                            ),
                            managed_tool_pairs_balanced=tool_pairs_balanced(
                                managed_messages
                            ),
                        )
                    )
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    count = len(results)
    baseline_correct = sum(item.baseline.exact_match for item in results)
    managed_correct = sum(item.managed.exact_match for item in results)
    baseline_tokens = sum(item.baseline.total_prompt_tokens for item in results)
    managed_tokens = sum(item.managed.total_prompt_tokens for item in results)
    baseline_chars = sum(item.baseline.first_request_chars for item in results)
    managed_chars = sum(item.managed.first_request_chars for item in results)
    return {
        "schema_version": "1.0",
        "evaluation_kind": "live_context_answer_quality_ab",
        "generated_at": datetime.now(UTC).isoformat(),
        "model": config.llm.default_model,
        "dataset_cases": len(dataset),
        "repetitions": repetitions,
        "observations_per_condition": count,
        "summary": {
            "baseline_exact_match": round(baseline_correct / count, 4) if count else 0.0,
            "managed_exact_match": round(managed_correct / count, 4) if count else 0.0,
            "exact_match_delta_points": round(
                (managed_correct - baseline_correct) / count * 100,
                2,
            )
            if count
            else 0.0,
            "baseline_total_prompt_tokens": baseline_tokens,
            "managed_total_prompt_tokens": managed_tokens,
            "total_prompt_token_change_pct": round(
                (managed_tokens / baseline_tokens - 1) * 100, 2
            )
            if baseline_tokens
            else 0.0,
            "first_request_active_char_reduction_pct": round(
                (1 - managed_chars / baseline_chars) * 100, 2
            )
            if baseline_chars
            else 0.0,
            "managed_offline_recoverability": round(
                sum(item.managed_recoverable_offline for item in results) / count,
                4,
            )
            if count
            else 0.0,
            "managed_tool_calls": sum(item.managed.tool_calls for item in results),
        },
        "cases": [asdict(item) for item in results],
        "limitations": [
            "Synthetic needle-retrieval questions measure context retention, "
            "not coding-task success.",
            "This is a small synthetic A/B; broader coding tasks are still required "
            "to claim general task-success improvement.",
            "Total prompt tokens include any read_artifact recovery calls, "
            "not only the first request.",
        ],
    }


async def run_offline(dataset: list[dict[str, Any]]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="kc-context-offline-") as temp:
        root = Path(temp)
        for case in dataset:
            artifact_dir = root / str(case["id"])
            messages = await _build_messages(case, managed=True, artifact_dir=artifact_dir)
            expected = str(case["expected"])
            visible = expected in json.dumps(messages, ensure_ascii=False)
            in_artifact = _artifact_contains(artifact_dir, expected)
            recoverable = _recoverable(
                messages,
                artifact_dir,
                expected,
                kind=str(case["kind"]),
            )
            balanced = tool_pairs_balanced(messages)
            requires_artifact = (
                case.get("placement") == "middle"
                and case["kind"] in {"large_tool_result", "large_tool_error"}
            )
            retrieval_boundary_valid = not requires_artifact or (
                not visible and in_artifact
            )
            cases.append(
                {
                    "case_id": case["id"],
                    "expected_visible": visible,
                    "expected_in_artifact": in_artifact,
                    "recoverable": recoverable,
                    "tool_pairs_balanced": balanced,
                    "retrieval_boundary_valid": retrieval_boundary_valid,
                    "passed": recoverable and balanced and retrieval_boundary_valid,
                }
            )
    return {
        "schema_version": "1.0",
        "evaluation_kind": "offline_context_recoverability",
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": {
            "cases": len(cases),
            "passed": sum(bool(case["passed"]) for case in cases),
        },
        "cases": cases,
        "limitations": [
            "This mode proves facts remain visible or recoverable from an artifact; "
            "it does not prove a model will retrieve them. Use --live for answer quality."
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B evaluate context answer quality")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--live", action="store_true", help="call the configured LLM")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    if args.repetitions < 1:
        parser.error("--repetitions must be at least 1")
    report = asyncio.run(
        run_live(dataset, repetitions=args.repetitions)
        if args.live
        else run_offline(dataset)
    )
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    summary = report["summary"]
    if args.live:
        return 0 if summary["managed_exact_match"] == 1.0 else 1
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
