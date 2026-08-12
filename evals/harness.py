from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

EVAL_ROOT = Path(__file__).resolve().parent
CASES_ROOT = EVAL_ROOT / "datasets" / "local_v1" / "cases"


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    language: str
    difficulty: str
    category: str
    split: str
    max_steps: int
    time_limit_s: int
    tags: list[str]
    directory: Path
    task: str

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["directory"] = str(self.directory)
        return data


@dataclass(frozen=True)
class Verification:
    case_id: str
    passed: bool
    returncode: int
    duration_ms: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_case(case_id: str) -> Case:
    directory = CASES_ROOT / case_id
    metadata_path = directory / "case.toml"
    task_path = directory / "task.md"
    if not metadata_path.exists() or not task_path.exists():
        raise KeyError(f"unknown case: {case_id}")
    metadata = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
    return Case(
        id=str(metadata["id"]),
        title=str(metadata["title"]),
        language=str(metadata["language"]),
        difficulty=str(metadata["difficulty"]),
        category=str(metadata["category"]),
        split=str(metadata["split"]),
        max_steps=int(metadata["max_steps"]),
        time_limit_s=int(metadata["time_limit_s"]),
        tags=[str(tag) for tag in metadata.get("tags", [])],
        directory=directory,
        task=task_path.read_text(encoding="utf-8").strip(),
    )


def iter_cases() -> list[Case]:
    if not CASES_ROOT.exists():
        return []
    return [
        load_case(path.name)
        for path in sorted(CASES_ROOT.iterdir())
        if path.is_dir() and (path / "case.toml").exists()
    ]


def prepare_case(case: Case, output: Path) -> None:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(case.directory / "seed", output, dirs_exist_ok=True)
    (output / "TASK.md").write_text(case.task + "\n", encoding="utf-8")


def apply_gold(case: Case, workspace: Path) -> None:
    solution = case.directory / "solution"
    if not solution.exists():
        raise FileNotFoundError(f"gold solution missing: {solution}")
    shutil.copytree(solution, workspace, dirs_exist_ok=True)


def verify_case(case: Case, workspace: Path) -> Verification:
    public_tests = workspace / "tests"
    hidden_tests = case.directory / "oracle"
    command = [sys.executable, "-m", "pytest", "-q"]
    if public_tests.exists():
        command.append(str(public_tests))
    command.append(str(hidden_tests))
    environment = os.environ.copy()
    old_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(workspace)
        if not old_pythonpath
        else os.pathsep.join((str(workspace), old_pythonpath))
    )
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=case.time_limit_s,
        check=False,
    )
    return Verification(
        case_id=case.id,
        passed=completed.returncode == 0,
        returncode=completed.returncode,
        duration_ms=int((time.monotonic() - started) * 1000),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def validate_dataset() -> int:
    reports: list[dict[str, Any]] = []
    all_valid = True
    for case in iter_cases():
        with tempfile.TemporaryDirectory(prefix=f"kc-eval-{case.id}-") as temp:
            workspace = Path(temp) / "workspace"
            prepare_case(case, workspace)
            baseline = verify_case(case, workspace)
            apply_gold(case, workspace)
            gold = verify_case(case, workspace)
        valid = not baseline.passed and gold.passed
        all_valid = all_valid and valid
        reports.append(
            {
                "case_id": case.id,
                "valid": valid,
                "baseline_failed": not baseline.passed,
                "gold_passed": gold.passed,
                "baseline_duration_ms": baseline.duration_ms,
                "gold_duration_ms": gold.duration_ms,
                "gold_stdout": "" if gold.passed else gold.stdout,
                "gold_stderr": "" if gold.passed else gold.stderr,
            }
        )
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0 if all_valid and reports else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KamaClaude local coding-agent eval harness")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list local evaluation cases")
    show = subparsers.add_parser("show", help="show one case and its task prompt")
    show.add_argument("case_id")
    prepare = subparsers.add_parser("prepare", help="materialize a clean agent workspace")
    prepare.add_argument("case_id")
    prepare.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="run public and hidden tests")
    verify.add_argument("case_id")
    verify.add_argument("--workspace", type=Path, required=True)
    subparsers.add_parser("validate", help="prove every seed fails and gold solution passes")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "list":
        payload = [case.public_dict() for case in iter_cases()]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "show":
        print(json.dumps(load_case(args.case_id).public_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "prepare":
        prepare_case(load_case(args.case_id), args.output.resolve())
        print(args.output.resolve())
        return 0
    if args.command == "verify":
        result = verify_case(load_case(args.case_id), args.workspace.resolve())
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0 if result.passed else 1
    return validate_dataset()


if __name__ == "__main__":
    raise SystemExit(main())
