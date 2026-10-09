"""Small local SWE-bench Lite pilot, with independent baseline/gold/candidate checks.

This is a local pytest verifier, not the official Docker evaluation harness.
Oracle patches stay outside the Agent workspace and are applied only to a fresh
archive of the base commit. The Agent gets the original issue and public tests.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shlex
import subprocess
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

CASES = {
    "pydicom__pydicom-1694": {
        "public_tests": ["pydicom/tests/test_json.py"],
        "dependencies": ["pytest==7.4.4", "numpy==1.23.5"],
    },
    "pylint-dev__astroid-1268": {
        "public_tests": ["tests/unittest_nodes.py"],
        "dependencies": ["pytest==7.4.4", "wrapt==1.13.3"],
    },
    "sqlfluff__sqlfluff-2419": {
        "public_tests": ["test/rules/yaml_test_cases_test.py", "-k", "L060"],
        "dependencies": ["pytest==7.4.4", "jinja2<3.1", "click<8.1"],
    },
}
TOOLS = [
    "read_file",
    "grep_search",
    "search_text",
    "list_dir",
    "edit_file",
    "apply_patch",
    "write_file",
    "run_tests",
    "git_diff",
    "read_artifact",
]


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def require(args: list[str], *, cwd: Path | None = None) -> None:
    result = command(args, cwd=cwd)
    if result.returncode:
        raise RuntimeError(f"command failed: {shlex.join(args)}\n{result.stdout}\n{result.stderr}")


def test_environment(root: Path, python: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    # Always import this checkout, never the editable package in another clone.
    env["PYTHONPATH"] = os.pathsep.join([str(root / "src"), str(root)])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("PYTEST_ADDOPTS", None)
    return env


def pytest_check(root: Path, python: Path, tests: list[str], output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    xml = output / "pytest.xml"
    started = time.monotonic()
    result = command(
        [str(python), "-m", "pytest", "-q", *tests, "--junitxml", str(xml)],
        cwd=root,
        env=test_environment(root, python),
    )
    (output / "pytest.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    if xml.exists():
        for test in ElementTree.parse(xml).iter("testcase"):
            if test.find("failure") is not None:
                counts["failed"] += 1
            elif test.find("error") is not None:
                counts["errors"] += 1
            elif test.find("skipped") is not None:
                counts["skipped"] += 1
            else:
                counts["passed"] += 1
    return {
        "returncode": result.returncode,
        "duration_s": round(time.monotonic() - started, 2),
        "tests": tests,
        **counts,
    }


def record_environment(python: Path, output: Path) -> None:
    result = command(
        [
            str(python),
            "-c",
            "import json,sys,importlib.metadata as m; "
            "print(json.dumps({'python':sys.version,'packages':"
            "{d.metadata['Name']:d.version for d in m.distributions()}}))",
        ]
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    save(output / "python_environment.json", json.loads(result.stdout))


def tests_passed(result: dict[str, Any]) -> bool:
    """A successful process without an executed assertion is not a passing check."""
    return result["returncode"] == 0 and result["passed"] > 0 and result["errors"] == 0


def audit(args: argparse.Namespace) -> None:
    """Check the actual delivery, including new tests excluded from official scoring."""
    output = args.output / args.case
    workspace = args.workspaces / args.case
    python = args.environments / args.case / "bin" / "python"
    record_environment(python, output)
    public = pytest_check(workspace, python, CASES[args.case]["public_tests"], output / "delivery")
    new_tests = [
        filename
        for filename in command(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace
        ).stdout.splitlines()
        if Path(filename).suffix == ".py"
        and Path(filename).name.startswith(("test_", "unittest_"))
        and any(part in {"test", "tests"} for part in Path(filename).parts[:-1])
    ]
    extra = (
        pytest_check(workspace, python, new_tests, output / "delivery_new_tests")
        if new_tests
        else None
    )
    result = {
        "public_tests": public,
        "new_tests": extra,
        "passed": public["returncode"] == 0 and (extra is None or extra["returncode"] == 0),
    }
    save(output / "delivery.json", result)
    print(json.dumps({"case_id": args.case, "delivery": result}), flush=True)


def archive_checkout(source: Path, commit: str, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite {destination}")
    destination.mkdir(parents=True)
    raw = subprocess.check_output(["git", "archive", commit], cwd=source)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(destination, filter="data")
    require(["git", "init", "-q"], cwd=destination)


def apply_patch(root: Path, patch: str) -> None:
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=root,
        input=patch,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"patch failed: {result.stderr}")


def restore_official_tests(source: Path, commit: str, root: Path, test_patch: str) -> None:
    """Discard candidate edits only to authoritative test files, in the verifier copy."""
    for line in test_patch.splitlines():
        if not line.startswith("+++ b/"):
            continue
        relative = line[6:]
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"test patch escapes verifier: {relative}")
        original = subprocess.run(
            ["git", "show", f"{commit}:{relative}"],
            cwd=source,
            capture_output=True,
            check=False,
        )
        if original.returncode == 0:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original.stdout)
        elif path.exists():
            path.unlink()  # Derived verifier copy; original Agent files are preserved.


def verify(
    case: dict[str, Any],
    workspace: Path,
    python: Path,
    output: Path,
    variant: str,
    patch: str = "",
    *,
    verifier_root: Path,
) -> dict[str, Any]:
    root = verifier_root / case["instance_id"] / variant
    archive_checkout(workspace, case["base_commit"], root)
    if patch:
        apply_patch(root, patch)
    restore_official_tests(workspace, case["base_commit"], root, case["test_patch"])
    apply_patch(root, case["test_patch"])
    target = pytest_check(
        root, python, json.loads(case["FAIL_TO_PASS"]), output / variant / "target"
    )
    files = sorted({node.split("::")[0] for node in json.loads(case["FAIL_TO_PASS"])})
    regression = pytest_check(root, python, files, output / variant / "regression")
    return {
        "target": target,
        "regression": regression,
        "passed": tests_passed(target) and tests_passed(regression),
    }


def prepare(args: argparse.Namespace) -> None:
    import pyarrow.parquet as pq

    rows = {r["instance_id"]: r for r in pq.read_table(args.dataset).to_pylist()}
    ids = args.cases or list(CASES)
    for case_id in ids:
        case = dict(rows[case_id])
        case["_dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
        case_output = args.output / case_id
        workspace = args.workspaces / case_id
        if workspace.exists():
            raise FileExistsError(f"refusing to reuse {workspace}")
        save(case_output / "case.json", case)
        (case_output / "task.md").write_text(case["problem_statement"], encoding="utf-8")
        (case_output / "official_test.patch").write_text(case["test_patch"], encoding="utf-8")
        workspace.mkdir(parents=True)
        require(["git", "init", "-q", str(workspace)])
        require(
            [
                "git",
                "fetch",
                "--depth",
                "1",
                f"https://github.com/{case['repo']}.git",
                case["base_commit"],
            ],
            cwd=workspace,
        )
        require(["git", "switch", "--detach", "FETCH_HEAD"], cwd=workspace)
        venv = args.environments / case_id
        require([args.uv, "venv", "--python", "3.10", str(venv)])
        python = venv / "bin" / "python"
        require(
            [
                args.uv,
                "pip",
                "install",
                "--python",
                str(python),
                *CASES[case_id]["dependencies"],
                "-e",
                str(workspace),
            ]
        )
        print(f"prepared {case_id}", flush=True)


async def agent_run(
    case: dict[str, Any], workspace: Path, python: Path, output: Path, max_steps: int, timeout: int
) -> dict[str, Any]:
    from kama_claude.core.compact.compactor import Compactor
    from kama_claude.core.compact.engine import ContextEngine, ContextPolicy
    from kama_claude.core.config import get_config
    from kama_claude.core.context import ExecutionContext
    from kama_claude.core.events.bus import EventBus
    from kama_claude.core.events.writer import EventWriter
    from kama_claude.core.llm.factory import build_provider
    from kama_claude.core.loop import AgentLoop
    from kama_claude.core.permissions.manager import PermissionManager
    from kama_claude.core.permissions.policy import PermissionDecision, ToolPolicy
    from kama_claude.core.runner import AgentRunner
    from kama_claude.core.task.manager import TaskManager
    from kama_claude.core.tools.artifacts import ToolArtifactStore
    from kama_claude.core.tools.builtin.run_tests import RunTestsTool
    from kama_claude.core.tools.file_versions import FileVersionTracker

    config = get_config()
    config.llm.fallback_providers = []  # Do not silently change the evaluation model.
    provider = build_provider(config.llm)
    bus = EventBus()
    run_id = output.parent.name
    context = ExecutionContext(run_id=run_id, goal=case["problem_statement"], max_steps=max_steps)
    artifacts = ToolArtifactStore(
        output / "artifacts",
        threshold=config.compaction.tool_result_limit,
        keep_chars=config.compaction.tool_result_keep,
    )
    runner = AgentRunner(config, provider=provider)
    registry = runner._build_registry(
        TaskManager(output / "tasks"),
        file_versions=FileVersionTracker(),
        workspace=workspace,
        artifact_store=artifacts,
        tool_whitelist=TOOLS,
    )
    public_tests = CASES[case["instance_id"]]["public_tests"]
    test_command = shlex.join([str(python), "-m", "pytest", "-q", *public_tests])

    class FixedTests(RunTestsTool):
        description = (
            "Run this checkout's existing relevant tests in the prepared Python environment. "
            "No command argument is needed."
        )
        input_schema = {"type": "object", "properties": {}, "required": []}

        async def invoke(self, params: dict[str, object]):
            return await super().invoke({"command": test_command, "timeout": 120})

    # The fixed test tool is evaluation-only; all production tools retain their implementations.
    registry.register(FixedTests(workspace))
    permission = PermissionManager({name: ToolPolicy(PermissionDecision.ALLOW) for name in TOOLS})
    engine = ContextEngine(
        Compactor(bus, output, ""),
        ContextPolicy(
            tool_result_limit=config.compaction.tool_result_limit,
            tool_result_keep=config.compaction.tool_result_keep,
            full_compact_threshold=config.compaction.auto_threshold,
        ),
    )
    loop = AgentLoop(
        provider,
        registry,
        bus,
        permission_manager=permission,
        context_engine=engine,
        artifact_store=artifacts,
    )

    async def progress(event):
        if event.type == "step.started":
            print(f"{case['instance_id']} step={event.step}", flush=True)
        elif event.type == "tool.call_started":
            print(f"  tool={event.tool_name}", flush=True)

    bus.subscribe(progress)
    old_environment = {key: os.environ.get(key) for key in ("PATH", "PYTHONPATH")}
    env = test_environment(workspace, python)
    os.environ.update({key: env[key] for key in old_environment})
    started = time.monotonic()
    try:
        async with EventWriter(output / "events.jsonl", run_id=run_id) as writer:
            writer.subscribe(bus)
            try:
                await asyncio.wait_for(loop.run(context), timeout=timeout)
            except TimeoutError:
                context.mark_failed("evaluation_timeout")
    finally:
        for key, value in old_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        close = getattr(provider, "close", None)
        if close is not None:
            await close()
    save(output / "messages.json", context.messages)
    events = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
    usage = [event for event in events if event.get("type") == "llm.usage"]
    # Cache fields are reported separately; input_tokens has provider-specific semantics.
    metrics = {
        "status": context.status,
        "reason": context.reason,
        "steps": context.step,
        "duration_s": round(time.monotonic() - started, 2),
        "final_answer": context.result,
        "tool_calls": sum(event.get("type") == "tool.call_started" for event in events),
        "tool_failures": sum(event.get("type") == "tool.call_failed" for event in events),
        "usage": {
            key: sum(event.get(key, 0) for event in usage)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
        },
        "tools": TOOLS,
        "max_steps": max_steps,
        "public_tests": public_tests,
        "providers": [
            {"name": spec.name, "kind": spec.kind, "model": spec.model}
            for spec in config.llm.providers
        ],
    }
    save(output / "run.json", metrics)
    return metrics


def run(args: argparse.Namespace) -> None:
    case_output = args.output / args.case
    case = json.loads((case_output / "case.json").read_text(encoding="utf-8"))
    workspace = args.workspaces / args.case
    python = args.environments / args.case / "bin" / "python"
    record_environment(python, case_output)
    if command(["git", "status", "--porcelain"], cwd=workspace).stdout.strip():
        raise RuntimeError("Agent workspace must start clean")
    report: dict[str, Any] = {
        "case_id": args.case,
        "base_commit": case["base_commit"],
        "started_at": datetime.now(UTC).isoformat(),
        "dataset_sha256": args.dataset_hash or case.get("_dataset_sha256", ""),
        "kc_commit": command(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "verifier": "local pytest; official test_patch; relevant test files",
    }
    report["baseline"] = verify(
        case,
        workspace,
        python,
        case_output,
        "baseline",
        verifier_root=args.verifiers,
    )
    report["gold"] = verify(
        case,
        workspace,
        python,
        case_output,
        "gold",
        case["patch"],
        verifier_root=args.verifiers,
    )
    report["public_preflight"] = pytest_check(
        workspace, python, CASES[args.case]["public_tests"], case_output / "public_preflight"
    )
    public = report["public_preflight"]
    public_valid = (
        public["returncode"] in {0, 1}
        and public["passed"] + public["failed"] > 0
        and public["errors"] == 0
    )
    report["environment_valid"] = (
        report["baseline"]["target"]["failed"] > 0
        and report["baseline"]["target"]["errors"] == 0
        and report["gold"]["passed"]
        and public_valid
    )
    save(case_output / "report.json", report)
    if not report["environment_valid"]:
        print(f"environment blocked {args.case}: inspect report.json", flush=True)
        return
    report["agent"] = asyncio.run(
        agent_run(case, workspace, python, case_output / "agent", args.max_steps, args.timeout)
    )
    # Include newly created files, without staging or committing anything.
    patch = command(["git", "diff", "--binary", "HEAD"], cwd=workspace).stdout
    for filename in command(
        ["git", "ls-files", "--others", "--exclude-standard"], cwd=workspace
    ).stdout.splitlines():
        patch += command(
            ["git", "diff", "--no-index", "--binary", "--", "/dev/null", filename], cwd=workspace
        ).stdout
    (case_output / "candidate.patch").write_text(patch, encoding="utf-8")
    report["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
    try:
        report["candidate"] = verify(
            case,
            workspace,
            python,
            case_output,
            "candidate",
            patch,
            verifier_root=args.verifiers,
        )
    except RuntimeError as exc:
        report["candidate"] = {"passed": False, "patch_apply_error": str(exc)}
    report["resolved"] = bool(patch.strip()) and report["candidate"]["passed"]
    report["finished_at"] = datetime.now(UTC).isoformat()
    save(case_output / "report.json", report)
    print(
        json.dumps(
            {
                "case_id": args.case,
                "resolved": report["resolved"],
                "run_status": report["agent"]["status"],
                "candidate": report["candidate"],
            }
        ),
        flush=True,
    )


def reverify(args: argparse.Namespace) -> None:
    case_output = args.output / args.case
    case = json.loads((case_output / "case.json").read_text(encoding="utf-8"))
    report = json.loads((case_output / "report.json").read_text(encoding="utf-8"))
    patch = (case_output / "candidate.patch").read_text(encoding="utf-8")
    report["previous_candidate"] = report["candidate"]
    report["candidate"] = verify(
        case,
        args.workspaces / args.case,
        args.environments / args.case / "bin" / "python",
        case_output,
        "candidate_authoritative_tests",
        patch,
        verifier_root=args.verifiers,
    )
    report["resolved"] = bool(patch.strip()) and report["candidate"]["passed"]
    save(case_output / "report.json", report)
    print(
        json.dumps(
            {"case_id": args.case, "resolved": report["resolved"], "candidate": report["candidate"]}
        ),
        flush=True,
    )


def summarize(args: argparse.Namespace) -> None:
    entries = []
    excluded = []
    for path in sorted(args.output.rglob("report.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        relative = str(path.relative_to(args.output))
        if "resolved" not in report:
            excluded.append({"report": relative, "reason": "incomplete_or_interrupted"})
            continue
        delivery = path.parent / "delivery.json"
        corrected = path.parent / "delivery_corrected.json"
        entries.append(
            {
                "case_id": report["case_id"],
                "report": relative,
                "environment_valid": report["environment_valid"],
                "resolved": report["resolved"],
                "target": report["candidate"].get("target"),
                "regression": report["candidate"].get("regression"),
                "run_status": report["agent"]["status"],
                "steps": report["agent"]["steps"],
                "tool_calls": report["agent"]["tool_calls"],
                "duration_s": report["agent"]["duration_s"],
                "delivery": json.loads(delivery.read_text()) if delivery.exists() else None,
                "posthoc_delivery": json.loads(corrected.read_text())
                if corrected.exists()
                else None,
            }
        )
    ids = [entry["case_id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate completed cases: select one attempt per issue explicitly")
    summary = {
        "completed_issues": len(entries),
        "resolved_issues": sum(entry["resolved"] for entry in entries),
        "cases": entries,
        "excluded": excluded,
        "scope": "convenience sample; local pytest verifier; not official SWE-bench score",
    }
    save(args.output / "summary.json", summary)
    print(json.dumps(summary), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run", "reverify", "audit", "summarize"])
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--dataset-hash", default="")
    parser.add_argument("--case", choices=list(CASES))
    parser.add_argument("--cases", nargs="*", choices=list(CASES))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspaces", type=Path, required=True)
    parser.add_argument("--environments", type=Path, required=True)
    parser.add_argument("--verifiers", type=Path)
    parser.add_argument("--uv", default="uv")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.action == "prepare" and args.dataset is None:
        parser.error("prepare requires --dataset")
    if args.action in {"run", "reverify", "audit"} and args.case is None:
        parser.error("run/reverify/audit requires --case")
    for key in ("output", "workspaces", "environments"):
        setattr(args, key, getattr(args, key).expanduser().resolve())
    args.verifiers = (
        args.verifiers.expanduser().resolve()
        if args.verifiers
        else args.environments.parent / "verifiers"
    )
    if args.action == "prepare":
        prepare(args)
    elif args.action == "summarize":
        summarize(args)
    elif args.action == "audit":
        audit(args)
    elif args.action == "reverify":
        reverify(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
