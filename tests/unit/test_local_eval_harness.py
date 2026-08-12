from pathlib import Path

from evals.harness import iter_cases, prepare_case


def test_local_eval_cases_have_unique_ids_and_complete_assets() -> None:
    cases = iter_cases()
    assert len(cases) == 6
    assert len({case.id for case in cases}) == len(cases)
    assert {case.split for case in cases} == {"dev", "holdout"}
    for case in cases:
        assert (case.directory / "seed").is_dir()
        assert (case.directory / "oracle").is_dir()
        assert (case.directory / "solution").is_dir()
        assert case.task.startswith("# ")


def test_prepare_case_excludes_oracle_and_solution(tmp_path: Path) -> None:
    case = next(case for case in iter_cases() if case.id == "config-precedence")
    workspace = tmp_path / "workspace"
    prepare_case(case, workspace)
    assert (workspace / "TASK.md").exists()
    assert (workspace / "app" / "config.py").exists()
    assert not (workspace / "oracle").exists()
    assert not (workspace / "solution").exists()
