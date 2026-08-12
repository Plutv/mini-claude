import json

from app.session_store import read_records


def test_truncated_tail_is_ignored(tmp_path) -> None:
    path = tmp_path / "thread.jsonl"
    path.write_text('{"role":"user","content":"hi"}\n{"role":', encoding="utf-8")
    assert read_records(path) == [{"role": "user", "content": "hi"}]


def test_valid_records_keep_order(tmp_path) -> None:
    path = tmp_path / "thread.jsonl"
    rows = [{"n": 1}, {"n": 2}]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert read_records(path) == rows
