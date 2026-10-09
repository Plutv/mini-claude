"""Verifier plumbing tests; no network access or model calls."""

import json
from argparse import Namespace
from pathlib import Path

import pytest
from evals import swe_lite_pilot as pilot


def test_restore_tests_uses_base_version_without_touching_source(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "test_example.py"
    original.write_text("original source", encoding="utf-8")
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    altered = verifier / "test_example.py"
    altered.write_text("candidate test edits", encoding="utf-8")

    def fake_run(args, **kwargs):
        assert args == ["git", "show", "base:test_example.py"]
        assert kwargs["cwd"] == source
        return pilot.subprocess.CompletedProcess(args, 0, stdout=b"base test", stderr=b"")

    monkeypatch.setattr(pilot.subprocess, "run", fake_run)
    pilot.restore_official_tests(source, "base", verifier, "+++ b/test_example.py\n")
    assert altered.read_text() == "base test"
    assert original.read_text() == "original source"


def test_restore_new_official_test_removes_only_derived_candidate_file(tmp_path, monkeypatch):
    verifier = tmp_path / "verifier"
    verifier.mkdir()
    test = verifier / "test_new.py"
    test.write_text("candidate version", encoding="utf-8")
    monkeypatch.setattr(
        pilot.subprocess,
        "run",
        lambda args, **kwargs: pilot.subprocess.CompletedProcess(args, 128, stdout=b"", stderr=b""),
    )
    pilot.restore_official_tests(tmp_path / "source", "base", verifier, "+++ b/test_new.py\n")
    assert not test.exists()


def test_restore_official_tests_rejects_escaping_path(tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        pilot.restore_official_tests(
            tmp_path, "base", tmp_path / "verifier", "+++ b/../outside.py\n"
        )


def test_test_environment_overrides_editable_import_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/wrong/checkout")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--wrong-option")
    python = Path("/prepared/bin/python")
    env = pilot.test_environment(tmp_path, python)
    assert env["PYTHONPATH"].split(pilot.os.pathsep) == [str(tmp_path / "src"), str(tmp_path)]
    assert "PYTEST_ADDOPTS" not in env
    assert env["PATH"].split(pilot.os.pathsep)[0] == str(python.parent)


@pytest.mark.parametrize(
    "returncode,passed,errors,expected",
    [
        (0, 3, 0, True),
        (0, 0, 0, False),
        (5, 0, 0, False),
        (1, 3, 1, False),
    ],
)
def test_passing_check_requires_executed_tests(returncode, passed, errors, expected):
    assert (
        pilot.tests_passed(
            {
                "returncode": returncode,
                "passed": passed,
                "errors": errors,
            }
        )
        is expected
    )


def test_summary_excludes_incomplete_attempts(tmp_path):
    pilot.save(tmp_path / "incomplete" / "report.json", {"case_id": "interrupted"})
    pilot.save(
        tmp_path / "finished" / "report.json",
        {
            "case_id": "fixed",
            "resolved": True,
            "environment_valid": True,
            "candidate": {"target": {"passed": 1}, "regression": {"passed": 3}},
            "agent": {"status": "success", "steps": 20, "tool_calls": 25, "duration_s": 60},
        },
    )
    pilot.summarize(Namespace(output=tmp_path))
    result = json.loads((tmp_path / "summary.json").read_text())
    assert result["completed_issues"] == 1
    assert result["resolved_issues"] == 1
    assert len(result["excluded"]) == 1
    assert result["cases"][0]["delivery"] is None
