from __future__ import annotations

import json

from evals.context_quality_eval import DEFAULT_DATASET, run_offline


async def test_context_quality_dataset_preserves_recoverability_boundaries() -> None:
    dataset = json.loads(DEFAULT_DATASET.read_text(encoding="utf-8"))

    report = await run_offline(dataset)

    assert report["summary"] == {"cases": 6, "passed": 6}
    cases = {case["case_id"]: case for case in report["cases"]}
    for case_id in ("large-result-middle", "large-error-middle"):
        assert cases[case_id]["expected_visible"] is False
        assert cases[case_id]["expected_in_artifact"] is True
        assert cases[case_id]["retrieval_boundary_valid"] is True
    assert all(case["tool_pairs_balanced"] for case in cases.values())
