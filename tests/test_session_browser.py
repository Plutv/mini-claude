"""Unit tests for kama_claude.tui.session_browser pure helpers.

These helpers are intentionally textual-free so they can be unit-tested
without a running TUI / display. Run with:

    python -m unittest tests.test_session_browser -v
"""

from __future__ import annotations

import unittest

from kama_claude.tui.session_browser import (
    _filter_resumable_sessions,
    _preview,
    _render_session_list,
    _resolve_resume_target,
    _status_label,
    flatten_tool_result_content,
    is_compacted_history,
    parse_history_for_render,
)


def _mk(
    session_id: str,
    mode: str = "chat",
    status: str = "idle",
    title: str = "",
    updated_at: str = "",
    run_count: int = 0,
    interrupted_reason: str | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "mode": mode,
        "status": status,
        "title": title,
        "updated_at": updated_at,
        "run_count": run_count,
        "interrupted_reason": interrupted_reason,
    }


class TestPreview(unittest.TestCase):
    def test_short_string_unchanged(self):
        self.assertEqual(_preview("hello", 50), "hello")

    def test_long_string_truncated_with_ellipsis(self):
        s = "x" * 60
        out = _preview(s, 50)
        self.assertEqual(out, "x" * 50 + "…")
        self.assertEqual(len(out), 51)


class TestFilterResumableSessions(unittest.TestCase):
    def test_excludes_current_session(self):
        sessions = [
            _mk("cur", updated_at="2026-08-18T10:00:00"),
            _mk("other", updated_at="2026-08-18T09:00:00"),
        ]
        out = _filter_resumable_sessions(sessions, "cur")
        self.assertEqual([s["session_id"] for s in out], ["other"])

    def test_excludes_non_chat_modes(self):
        sessions = [
            _mk("a", mode="chat", updated_at="2026-08-18T10:00:00"),
            _mk("b", mode="agent", updated_at="2026-08-18T11:00:00"),
        ]
        out = _filter_resumable_sessions(sessions, None)
        self.assertEqual([s["session_id"] for s in out], ["a"])

    def test_sorts_by_updated_at_desc(self):
        sessions = [
            _mk("old", updated_at="2026-08-18T08:00:00"),
            _mk("new", updated_at="2026-08-18T12:00:00"),
            _mk("mid", updated_at="2026-08-18T10:00:00"),
        ]
        out = _filter_resumable_sessions(sessions, None)
        self.assertEqual([s["session_id"] for s in out], ["new", "mid", "old"])

    def test_current_id_none_keeps_all_chat(self):
        sessions = [_mk("a", updated_at="2026-08-18T10:00:00")]
        out = _filter_resumable_sessions(sessions, None)
        self.assertEqual(len(out), 1)


class TestResolveResumeTarget(unittest.TestCase):
    def test_empty_arg_returns_none(self):
        self.assertIsNone(_resolve_resume_target("", []))

    def test_digit_within_range(self):
        recent = [_mk("id-1"), _mk("id-2"), _mk("id-3")]
        self.assertEqual(_resolve_resume_target("2", recent), "id-2")

    def test_digit_out_of_range_low(self):
        recent = [_mk("id-1")]
        self.assertIsNone(_resolve_resume_target("0", recent))

    def test_digit_out_of_range_high(self):
        recent = [_mk("id-1")]
        self.assertIsNone(_resolve_resume_target("5", recent))

    def test_non_digit_passthrough(self):
        recent = [_mk("id-1")]
        self.assertEqual(
            _resolve_resume_target("abc-123", recent), "abc-123"
        )

    def test_resolved_value_is_str(self):
        recent = [_mk("id-1")]
        self.assertIsInstance(_resolve_resume_target("1", recent), str)


class TestRenderSessionList(unittest.TestCase):
    def test_includes_header_and_entries(self):
        recent = [
            _mk("id-1", status="idle", title="First chat", run_count=3),
            _mk("id-2", status="interrupted", title="Second", run_count=1,
                interrupted_reason="permission_denied"),
        ]
        out = _render_session_list(recent)
        self.assertIn("recent chat sessions", out)
        self.assertIn("1.", out)
        self.assertIn("id-1", out)
        self.assertIn("id-2", out)
        self.assertIn("(有历史 3 轮)", out)
        self.assertIn("(permission_denied)", out)

    def test_truncates_long_title(self):
        long_title = "x" * 80
        recent = [_mk("id-1", title=long_title)]
        out = _render_session_list(recent)
        self.assertIn("x" * 50 + "…", out)
        self.assertNotIn("x" * 80, out)

    def test_empty_list_still_shows_header(self):
        out = _render_session_list([])
        self.assertIn("recent chat sessions", out)


class TestFlattenToolResultContent(unittest.TestCase):
    def test_str_passthrough(self):
        self.assertEqual(flatten_tool_result_content("hello"), "hello")

    def test_list_of_text_dicts(self):
        content = [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
        ]
        self.assertEqual(flatten_tool_result_content(content), "line one\nline two")

    def test_list_with_mixed_types(self):
        content = [
            {"type": "text", "text": "a"},
            {"type": "image", "source": {}},
        ]
        self.assertEqual(flatten_tool_result_content(content), "a")

    def test_other_type_returns_str(self):
        self.assertEqual(flatten_tool_result_content(42), "42")


class TestParseHistoryForRender(unittest.TestCase):
    def test_user_text_string(self):
        items = parse_history_for_render([
            {"role": "user", "content": "hello there"},
        ])
        self.assertEqual(items, [{"kind": "user_text", "text": "hello there"}])

    def test_assistant_text_string(self):
        items = parse_history_for_render([
            {"role": "assistant", "content": "hi back"},
        ])
        self.assertEqual(items, [{"kind": "assistant_text", "text": "hi back"}])

    def test_tool_use_then_tool_result_pairing(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "text", "text": "let me read it"},
                {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "file contents",
                 "is_error": False},
            ]},
        ]
        items = parse_history_for_render(messages)
        self.assertEqual(items[0], {"kind": "assistant_text", "text": "let me read it"})
        self.assertEqual(items[1]["kind"], "tool_use")
        self.assertEqual(items[1]["tool_use_id"], "tu_1")
        self.assertEqual(items[1]["name"], "read_file")
        self.assertEqual(items[1]["input"], {"path": "a.py"})
        self.assertEqual(items[2]["kind"], "tool_result")
        self.assertEqual(items[2]["tool_use_id"], "tu_1")
        self.assertEqual(items[2]["content"], "file contents")
        self.assertFalse(items[2]["is_error"])

    def test_tool_result_error_flag(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_2", "name": "bash", "input": {"command": "x"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_2",
                 "content": [{"type": "text", "text": "boom"}], "is_error": True},
            ]},
        ]
        items = parse_history_for_render(messages)
        self.assertTrue(items[1]["is_error"])
        self.assertEqual(items[1]["content"], "boom")

    def test_tool_result_without_matching_tool_use(self):
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "orphan", "content": "lost", "is_error": False},
            ]},
        ]
        items = parse_history_for_render(messages)
        self.assertEqual(items[0]["kind"], "tool_result")
        self.assertEqual(items[0]["tool_use_id"], "orphan")

    def test_orphan_tool_use_no_result(self):
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_x", "name": "note_save", "input": {"content": "remember"}},
            ]},
        ]
        items = parse_history_for_render(messages)
        self.assertEqual(items[0]["kind"], "tool_use")
        self.assertEqual(items[0]["input"], {"content": "remember"})

    def test_unknown_role_ignored(self):
        messages = [
            {"role": "system", "content": "should be skipped"},
            {"role": "user", "content": "keep me"},
        ]
        items = parse_history_for_render(messages)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "user_text")

    def test_empty_content_list_skips_blanks(self):
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ]
        items = parse_history_for_render(messages)
        self.assertEqual(items, [])


class TestStatusLabel(unittest.TestCase):
    def test_known_statuses(self):
        self.assertEqual(_status_label("active"), ("empty", "yellow"))
        self.assertEqual(_status_label("waiting_for_input"), ("ready", "green"))
        self.assertEqual(_status_label("interrupted"), ("interrupted", "red"))
        self.assertEqual(_status_label("closed"), ("closed", "dim"))
        self.assertEqual(_status_label("running"), ("running", "yellow"))

    def test_unknown_status_falls_back(self):
        self.assertEqual(_status_label("weird"), ("weird", "dim"))


class TestRenderSessionListFriendly(unittest.TestCase):
    def _mk(self, session_id, status, runs, title="", reason=None):
        s = {
            "session_id": session_id,
            "mode": "chat",
            "status": status,
            "title": title,
            "run_count": runs,
            "interrupted_reason": reason,
        }
        if reason is not None:
            s["interrupted_reason"] = reason
        return s

    def test_empty_session_shows_empty_and_no_history(self):
        out = _render_session_list([
            self._mk("sess-a", "active", 0, "no chat yet"),
        ])
        self.assertIn("empty", out)
        self.assertIn("(空)", out)
        self.assertNotIn("(有历史", out)

    def test_ready_session_shows_history_count(self):
        out = _render_session_list([
            self._mk("sess-b", "waiting_for_input", 5, "你好"),
        ])
        self.assertIn("ready", out)
        self.assertIn("(有历史 5 轮)", out)

    def test_interrupted_shows_reason(self):
        out = _render_session_list([
            self._mk("sess-c", "interrupted", 0, "", reason="daemon_restarted"),
        ])
        self.assertIn("daemon_restarted", out)

    def test_legend_present(self):
        out = _render_session_list([self._mk("sess-d", "closed", 1, "x")])
        self.assertIn("ready=有历史可续", out)
        self.assertIn("empty=空会话", out)

    def test_index_prefix(self):
        out = _render_session_list([
            self._mk("sess-a", "active", 0),
            self._mk("sess-b", "waiting_for_input", 3, "hi"),
        ])
        self.assertIn("1.", out)
        self.assertIn("2.", out)


class TestIsCompactedHistory(unittest.TestCase):
    _ACK = "Understood, I'll continue from this summary."

    def _compacted(self, summary: str = "[Previous conversation summary]\n...") -> list[dict]:
        return [
            {"role": "user", "content": summary},
            {"role": "assistant", "content": self._ACK},
        ]

    def test_detects_auto_compaction(self):
        self.assertTrue(is_compacted_history(self._compacted()))

    def test_detects_manual_compaction(self):
        msgs = [
            {"role": "user", "content": "这是手动压缩的摘要文本"},
            {"role": "assistant", "content": self._ACK},
        ]
        self.assertTrue(is_compacted_history(msgs))

    def test_detects_block_content_ack(self):
        msgs = [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": [{"type": "text", "text": self._ACK}]},
        ]
        self.assertTrue(is_compacted_history(msgs))

    def test_normal_history_not_flagged(self):
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
        ]
        self.assertFalse(is_compacted_history(msgs))

    def test_too_short_not_flagged(self):
        self.assertFalse(is_compacted_history([{"role": "user", "content": "hi"}]))

    def test_wrong_second_role_not_flagged(self):
        msgs = [
            {"role": "user", "content": "x"},
            {"role": "user", "content": self._ACK},
        ]
        self.assertFalse(is_compacted_history(msgs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
