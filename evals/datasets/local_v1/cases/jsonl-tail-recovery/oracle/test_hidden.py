import json

import pytest

from app.session_store import read_records


def test_malformed_middle_row_is_not_silently_skipped(tmp_path) -> None:
    path = tmp_path / "thread.jsonl"
    path.write_text('{"n":1}\nnot-json\n{"n":3}\n', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_records(path)


def test_blank_lines_and_unicode_are_preserved_correctly(tmp_path) -> None:
    path = tmp_path / "thread.jsonl"
    path.write_text('\n{"content":"你好"}\n\n{"content":"完成"}\n', encoding="utf-8")
    assert read_records(path) == [{"content": "你好"}, {"content": "完成"}]
