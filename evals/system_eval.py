from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kama_claude.core.compact.budget import tool_pairs_balanced
from kama_claude.core.compact.engine import ContextEngine, ContextPolicy
from kama_claude.core.context import ExecutionContext
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats
from kama_claude.core.memory.store import MemoryStore
from kama_claude.core.plan.controller import PlanController
from kama_claude.core.session.store import SessionStore
from kama_claude.core.tools.artifacts import ToolArtifactStore
from kama_claude.core.tools.base import BaseTool, ToolResult
from kama_claude.core.tools.builtin.edit_file import EditFileTool
from kama_claude.core.tools.builtin.read_file import ReadFileTool
from kama_claude.core.tools.builtin.search_text import SearchTextTool


@dataclass(frozen=True)
class EvalCaseResult:
    suite: str
    case_id: str
    passed: bool
    metrics: dict[str, int | float | str | bool]


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
        raise AssertionError("context evaluation must not call the provider")


def _pass_rate(cases: list[EvalCaseResult]) -> float:
    if not cases:
        return 0.0
    return round(sum(case.passed for case in cases) / len(cases), 4)


def _suite_payload(cases: list[EvalCaseResult]) -> dict[str, Any]:
    return {
        "summary": {
            "cases": len(cases),
            "passed": sum(case.passed for case in cases),
            "pass_rate": _pass_rate(cases),
        },
        "cases": [asdict(case) for case in cases],
    }


def _active_chars(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))


def _tool_history(count: int, result_chars: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "inspect repository"}]
    for index in range(count):
        tool_id = f"read-{index}"
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_id,
                            "name": "read_file",
                            "input": {"path": f"src/module_{index}.py"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": f"file-{index}:" + "x" * result_chars,
                        }
                    ],
                },
            ]
        )
    messages.append({"role": "assistant", "content": "Previous inspection complete."})
    messages.append(
        {
            "role": "user",
            "content": "CURRENT_REQUEST_SENTINEL: fix the parser without changing its API",
        }
    )
    return messages


async def _artifact_case(
    root: Path,
    *,
    case_id: str,
    is_error: bool,
) -> EvalCaseResult:
    content = ("Traceback\n" if is_error else "tool output\n") + "x" * 100_000
    store = ToolArtifactStore(root / case_id, threshold=32_768)
    call = ToolCallBlock(id=case_id, name="bash", input={"command": "demo"})
    compacted = await store.externalize(
        call,
        ToolResult(
            content=content,
            is_error=is_error,
            error_type="runtime_error" if is_error else None,
        ),
    )
    artifact = next((root / case_id).glob("*.txt"))
    restored = artifact.read_text(encoding="utf-8")
    original_bytes = len(content.encode("utf-8"))
    active_bytes = len(compacted.content.encode("utf-8"))
    digest = hashlib.sha256(restored.encode("utf-8")).hexdigest()
    metadata = json.loads(artifact.with_suffix(".json").read_text(encoding="utf-8"))
    reduction = 1 - active_bytes / original_bytes
    passed = bool(
        restored == content
        and metadata["sha256"] == digest
        and compacted.is_error is is_error
        and compacted.error_type == ("runtime_error" if is_error else None)
    )
    return EvalCaseResult(
        suite="context",
        case_id=case_id,
        passed=passed,
        metrics={
            "original_bytes": original_bytes,
            "active_context_bytes": active_bytes,
            "reduction_pct": round(reduction * 100, 2),
            "artifact_sha256_valid": metadata["sha256"] == digest,
            "error_semantics_preserved": compacted.is_error is is_error,
        },
    )


async def evaluate_context(root: Path) -> list[EvalCaseResult]:
    cases = [
        await _artifact_case(root, case_id="large-success", is_error=False),
        await _artifact_case(root, case_id="large-error", is_error=True),
    ]

    messages = _tool_history(count=8, result_chars=12_000)
    before_chars = _active_chars(messages)
    context = ExecutionContext(run_id="eval-context", goal="inspect", max_steps=10)
    context.messages = messages
    context.step = 4
    engine = ContextEngine(
        None,
        ContextPolicy(
            context_window_tokens=200_000,
            full_compact_threshold=0.0,
            keep_recent_tool_results=3,
            microcompact_every_steps=4,
        ),
        clock=lambda: 100.0,
    )
    engine.record_usage(UsageStats(150_000, 100, context_pct=0.75))
    maintenance = await engine.prepare(context, _UnusedProvider())
    after_chars = _active_chars(context.messages)
    current_request_preserved = context.messages[-1] == messages[-1]
    pairs_balanced = tool_pairs_balanced(context.messages[:-1])
    cases.append(
        EvalCaseResult(
            suite="context",
            case_id="layered-maintenance",
            passed=bool(
                after_chars < before_chars
                and current_request_preserved
                and pairs_balanced
                and maintenance.changed
            ),
            metrics={
                "before_active_chars": before_chars,
                "after_active_chars": after_chars,
                "reduction_pct": round((1 - after_chars / before_chars) * 100, 2),
                "budgeted_results": maintenance.budgeted_results,
                "microcompacted_results": maintenance.microcompacted_results,
                "tool_pairs_balanced": pairs_balanced,
                "current_request_preserved": current_request_preserved,
            },
        )
    )
    return cases


def evaluate_memory(root: Path) -> list[EvalCaseResult]:
    store = MemoryStore(root / "memory.sqlite3")
    fixtures = [
        ("Use ruff to format Python code", ["python", "formatter"]),
        ("The application database is PostgreSQL", ["database", "postgresql"]),
        ("Run pytest for the unit test suite", ["python", "pytest"]),
        ("User prefers concise Chinese answers", ["中文", "回答"]),
        ("Use uv for Python dependency management", ["python", "uv"]),
    ]
    expected: dict[str, str] = {}
    for content, tags in fixtures:
        record = store.save(
            scope="project:eval",
            content=content,
            tags=tags,
            importance=0.9,
        )
        expected[tags[-1]] = record.id
    store.save(
        scope="session:other",
        content="Use black to format Python code",
        tags=["python", "formatter"],
        importance=1.0,
    )
    queries = [
        ("python formatter ruff", "formatter"),
        ("database postgresql", "postgresql"),
        ("run pytest tests", "pytest"),
        ("中文回答", "回答"),
        ("python dependency uv", "uv"),
    ]
    top1_hits = 0
    top3_hits = 0
    scope_violations = 0
    for query, key in queries:
        recalled = store.recall(
            query,
            scopes=["global", "project:eval", "session:current"],
            limit=3,
            min_score=0.1,
        )
        ids = [item.id for item in recalled]
        top1_hits += bool(ids and ids[0] == expected[key])
        top3_hits += expected[key] in ids
        scope_violations += sum(item.scope == "session:other" for item in recalled)

    retrieval = EvalCaseResult(
        suite="memory",
        case_id="selective-retrieval",
        passed=top1_hits == len(queries) and scope_violations == 0,
        metrics={
            "queries": len(queries),
            "hit_at_1": round(top1_hits / len(queries), 4),
            "hit_at_3": round(top3_hits / len(queries), 4),
            "scope_violations": scope_violations,
        },
    )

    first = store.save(scope="global", content="Prefer concise answers", tags=["style"])
    second = store.save(
        scope="global",
        content="Prefer concise answers",
        tags=["preference"],
        importance=0.9,
    )
    dedup = EvalCaseResult(
        suite="memory",
        case_id="content-deduplication",
        passed=first.id == second.id,
        metrics={
            "same_memory_id": first.id == second.id,
            "merged_tag_count": len(second.tags),
        },
    )

    budget_store = MemoryStore(root / "budget-memory.sqlite3")
    budget_store.save(scope="global", content="python " + "a" * 80, importance=1.0)
    budget_store.save(scope="global", content="python " + "b" * 80, importance=0.9)
    bounded = budget_store.recall(
        "python",
        scopes=["global"],
        limit=5,
        min_score=0.1,
        max_chars=100,
    )
    used_chars = sum(len(item.content) for item in bounded)
    budget = EvalCaseResult(
        suite="memory",
        case_id="recall-character-budget",
        passed=used_chars <= 100 and len(bounded) == 1,
        metrics={"selected": len(bounded), "used_chars": used_chars, "max_chars": 100},
    )
    return [retrieval, dedup, budget]


def _balanced_history(label: str) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": f"inspect {label}"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"tool-{label}",
                    "name": "read_file",
                    "input": {"path": f"{label}.py"},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": f"tool-{label}",
                    "content": f"content-{label}",
                }
            ],
        },
        {"role": "assistant", "content": f"finished {label}"},
    ]


def _recovery_result(
    case_id: str,
    passed: bool,
    **metrics: int | float | str | bool,
) -> EvalCaseResult:
    return EvalCaseResult("recovery", case_id, bool(passed), metrics)


def evaluate_recovery(root: Path) -> list[EvalCaseResult]:
    cases: list[EvalCaseResult] = []

    store = SessionStore(root / "roundtrip")
    baseline = _balanced_history("baseline")
    store.write_messages_atomic("session", baseline)
    restored = store.read_messages("session")
    cases.append(
        _recovery_result(
            "atomic-roundtrip",
            restored == baseline and tool_pairs_balanced(restored),
            messages_restored=len(restored),
            tool_pairs_balanced=tool_pairs_balanced(restored),
        )
    )

    unbalanced_store = SessionStore(root / "unbalanced")
    unbalanced_store.write_messages_atomic("session", baseline)
    unbalanced = baseline[:-2]
    rejected = False
    try:
        unbalanced_store.write_messages_atomic("session", unbalanced)
    except ValueError:
        rejected = True
    unchanged = unbalanced_store.read_messages("session") == baseline
    cases.append(
        _recovery_result(
            "reject-unbalanced-snapshot",
            rejected and unchanged,
            rejected=rejected,
            previous_snapshot_preserved=unchanged,
        )
    )

    failure_store = SessionStore(root / "replace-failure")
    failure_store.write_messages_atomic("session", baseline)
    replacement_failed = False
    try:
        with patch(
            "kama_claude.core.session.store.os.replace",
            side_effect=OSError("injected replace failure"),
        ):
            failure_store.write_messages_atomic("session", _balanced_history("new"))
    except OSError:
        replacement_failed = True
    previous_preserved = failure_store.read_messages("session") == baseline
    temp_files = list(failure_store.session_dir("session").glob("*.tmp"))
    cases.append(
        _recovery_result(
            "atomic-replace-failure",
            replacement_failed and previous_preserved and not temp_files,
            failure_injected=replacement_failed,
            previous_snapshot_preserved=previous_preserved,
            leaked_temp_files=len(temp_files),
        )
    )

    broken_store = SessionStore(root / "broken-tail")
    broken_store.write_messages_atomic("session", baseline)
    thread = broken_store.session_dir("session") / "thread.jsonl"
    with thread.open("a", encoding="utf-8") as handle:
        handle.write('{"role":"assistant","content":')
    broken_restored = broken_store.read_messages("session")
    cases.append(
        _recovery_result(
            "broken-jsonl-tail",
            broken_restored == baseline,
            valid_messages_restored=len(broken_restored),
            broken_tail_skipped=broken_restored == baseline,
        )
    )

    orphan_store = SessionStore(root / "orphan-tail")
    orphan_store.write_messages_atomic("session", baseline)
    orphan_thread = orphan_store.session_dir("session") / "thread.jsonl"
    orphan = {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "orphan-tool",
                "name": "bash",
                "input": {"command": "echo unsafe"},
            }
        ],
    }
    with orphan_thread.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(orphan) + "\n")
    orphan_restored = orphan_store.read_messages("session")
    cases.append(
        _recovery_result(
            "orphan-tool-use-tail",
            orphan_restored == baseline and tool_pairs_balanced(orphan_restored),
            orphan_trimmed=orphan_restored == baseline,
            tool_pairs_balanced=tool_pairs_balanced(orphan_restored),
        )
    )
    return cases


class _GovernanceTool(BaseTool):
    description = "evaluation tool"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, name: str, *, read_only: bool) -> None:
        self.name = name
        self.read_only = read_only

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        del params
        return ToolResult(content="ok")


async def evaluate_governance(root: Path) -> list[EvalCaseResult]:
    cases: list[EvalCaseResult] = []
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    outside = root / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    escape_blocked = False
    try:
        await ReadFileTool(workspace=workspace).invoke({"path": str(outside)})
    except PermissionError:
        escape_blocked = True
    cases.append(
        EvalCaseResult(
            "governance",
            "workspace-boundary",
            escape_blocked,
            {"absolute_escape_blocked": escape_blocked},
        )
    )

    target = workspace / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    replace_failed = False
    try:
        with patch(
            "kama_claude.core.tools.builtin.edit_file.os.replace",
            side_effect=OSError("injected replace failure"),
        ):
            await EditFileTool(workspace=workspace).invoke(
                {
                    "path": "app.py",
                    "old_text": "value = 1",
                    "new_text": "value = 2",
                }
            )
    except OSError:
        replace_failed = True
    original_preserved = target.read_text(encoding="utf-8") == "value = 1\n"
    leaked_temps = len(list(workspace.glob(".*.tmp")))
    cases.append(
        EvalCaseResult(
            "governance",
            "atomic-edit-rollback",
            replace_failed and original_preserved and leaked_temps == 0,
            {
                "failure_injected": replace_failed,
                "original_preserved": original_preserved,
                "leaked_temp_files": leaked_temps,
            },
        )
    )

    controller = PlanController(root / "plan.json")
    controller.enter("inspect before changing")
    serialized_read = _GovernanceTool("remote_read", read_only=True)
    mutation = _GovernanceTool("write", read_only=False)
    read_allowed = controller.guard(serialized_read) is None
    mutation_blocked = controller.guard(mutation) is not None
    cases.append(
        EvalCaseResult(
            "governance",
            "plan-safety-contract",
            read_allowed and mutation_blocked,
            {
                "serialized_read_allowed": read_allowed,
                "mutation_blocked": mutation_blocked,
            },
        )
    )

    (workspace / "matches.txt").write_text("hit\nhit\nhit\nhit\n", encoding="utf-8")
    search = await SearchTextTool(workspace).invoke(
        {"query": "hit", "max_results": 2}
    )
    match_count = search.content.count("matches.txt:")
    truncated = search.content.endswith("[results truncated]")
    cases.append(
        EvalCaseResult(
            "governance",
            "search-result-budget",
            match_count == 2 and truncated,
            {"returned_matches": match_count, "truncated": truncated},
        )
    )
    return cases


def _git_state() -> tuple[str, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return "unknown", True


async def run_evaluation(selected: set[str] | None = None) -> dict[str, Any]:
    selected = selected or {"context", "memory", "recovery", "governance"}
    suites: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="kc-system-eval-") as temp:
        root = Path(temp)
        if "context" in selected:
            suites["context"] = _suite_payload(await evaluate_context(root / "context"))
        if "memory" in selected:
            suites["memory"] = _suite_payload(evaluate_memory(root / "memory"))
        if "recovery" in selected:
            suites["recovery"] = _suite_payload(evaluate_recovery(root / "recovery"))
        if "governance" in selected:
            suites["governance"] = _suite_payload(
                await evaluate_governance(root / "governance")
            )

    total_cases = sum(int(suite["summary"]["cases"]) for suite in suites.values())
    passed = sum(int(suite["summary"]["passed"]) for suite in suites.values())
    commit, dirty = _git_state()
    return {
        "schema_version": "1.0",
        "evaluation_kind": "deterministic_system_capability",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "git_dirty": dirty,
        "environment": {"python": sys.version.split()[0], "platform": platform.platform()},
        "limitations": [
            "Uses deterministic synthetic fixtures and does not measure "
            "end-to-end model task success.",
            "Memory metrics measure retrieval quality, not observed reduction in model tool calls.",
            "Recovery metrics cover storage boundaries, not external tool exactly-once semantics.",
        ],
        "summary": {
            "suites": len(suites),
            "cases": total_cases,
            "passed": passed,
            "pass_rate": round(passed / total_cases, 4) if total_cases else 0.0,
        },
        "suites": suites,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate KC runtime system capabilities")
    parser.add_argument(
        "--suite",
        choices=("all", "context", "memory", "recovery", "governance"),
        default="all",
    )
    parser.add_argument("--output", type=Path, help="write the raw JSON report")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    selected = None if args.suite == "all" else {str(args.suite)}
    report = asyncio.run(run_evaluation(selected))
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["summary"]["pass_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
