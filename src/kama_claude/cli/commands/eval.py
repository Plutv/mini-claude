from __future__ import annotations

import json
from pathlib import Path

from kama_claude.core.eval import evaluate_trajectory


def cmd_eval(events_path: str) -> None:
    metrics = evaluate_trajectory(Path(events_path))
    print(json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2))
