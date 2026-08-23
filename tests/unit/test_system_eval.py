from __future__ import annotations

import json
from pathlib import Path

import pytest
from evals.system_eval import main, run_evaluation


async def test_system_evaluation_reports_all_capabilities() -> None:
    report = await run_evaluation()

    assert report["evaluation_kind"] == "deterministic_system_capability"
    assert report["summary"] == {
        "suites": 3,
        "cases": 11,
        "passed": 11,
        "pass_rate": 1.0,
    }
    assert set(report["suites"]) == {"context", "memory", "recovery"}
    assert report["limitations"]


async def test_context_metrics_cover_reduction_and_protocol_safety() -> None:
    report = await run_evaluation({"context"})
    cases = {
        case["case_id"]: case for case in report["suites"]["context"]["cases"]
    }

    assert cases["large-error"]["metrics"]["error_semantics_preserved"] is True
    assert cases["large-error"]["metrics"]["artifact_sha256_valid"] is True
    assert cases["layered-maintenance"]["metrics"]["tool_pairs_balanced"] is True
    assert cases["layered-maintenance"]["metrics"]["current_request_preserved"] is True
    assert cases["layered-maintenance"]["metrics"]["reduction_pct"] > 50


def test_cli_writes_machine_readable_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["system_eval", "--suite", "recovery", "--output", str(output)])

    exit_code = main()

    assert exit_code == 0
    saved = json.loads(output.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert saved == printed
    assert saved["summary"]["cases"] == 5
