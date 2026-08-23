from __future__ import annotations

from pathlib import Path

import pytest

from kama_claude.core.bus.envelope import HandlerError
from kama_claude.core.events.bus import EventBus
from kama_claude.core.runner import RunOutcome
from kama_claude.core.session.manager import (
    SESSION_CLOSED,
    SESSION_NOT_FOUND,
    SESSION_NOT_RESUMABLE,
    SessionManager,
)
from kama_claude.core.session.model import Session
from kama_claude.core.session.store import SessionStore


class _Runner:
    # 模拟 AgentRunner，将 run 新消息写入 thread 后返回成功
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        assert run_id is not None
        assert session is not None
        assert store is not None
        store.append_messages(
            session.id,
            [{"role": "assistant", "content": [{"type": "text", "text": f"done {goal}"}]}],
            run_id,
        )
        return RunOutcome(status="success", result="done", reason=None)


# 功能：验证 create 会创建 active session、写入 meta 并发布 session.created 事件
# 设计：用真实 SessionStore + EventBus 收集事件，覆盖 manager 与 store/bus 的协作边界
async def test_create_session_writes_meta_and_event(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]

    session = await manager.create("chat", "title")

    assert session.status == "active"
    assert store.read_meta(session.id).title == "title"
    assert [e.type for e in events] == ["session.created"]  # type: ignore[attr-defined]


# 功能：验证 chat session 处理一条消息后进入 waiting_for_input，并保留 user/assistant thread
# 设计：mock runner 主动追加 assistant 消息，确认 send_message 负责 user 消息、状态流转和 run_id 记录
async def test_send_message_chat_enters_waiting_and_writes_thread(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")

    run_id = await manager.send_message(session.id, "hello")

    loaded = store.read_meta(session.id)
    assert loaded.status == "waiting_for_input"
    assert loaded.run_ids == [run_id]
    messages = store.read_messages(session.id)
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[1]["role"] == "assistant"


# 功能：验证 one_shot session 在单次消息完成后自动 closed
# 设计：复用 mock runner 的成功路径，聚焦 mode 对最终状态的影响，保证 kama run 的统一路径正确
async def test_one_shot_auto_closes(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    await manager.send_message(session.id, "hello")

    assert store.read_meta(session.id).status == "closed"


# 功能：验证不存在的 session_id 返回 session_not_found 错误码
# 设计：直接调用 get_history 的查找路径，断言 HandlerError code，覆盖 IPC handler 可结构化返回错误
async def test_missing_session_raises_handler_error(tmp_path: Path) -> None:
    manager = SessionManager(SessionStore(tmp_path), lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    with pytest.raises(HandlerError) as exc:
        await manager.get_history("missing")
    assert exc.value.code == SESSION_NOT_FOUND


# 功能：验证 closed session 不能继续 send_message
# 设计：先显式 close，再发送消息，断言 session_closed 错误码，覆盖状态机拒绝路径
async def test_closed_session_rejects_message(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("chat")
    await manager.close(session.id)

    with pytest.raises(HandlerError) as exc:
        await manager.send_message(session.id, "again")
    assert exc.value.code == SESSION_CLOSED


async def test_manager_rebuilds_index_and_marks_crashed_run_interrupted(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path)
    crashed = Session(
        id="sess-crashed",
        mode="chat",
        status="running",
        title="recover me",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:01:00+00:00",
        run_ids=["run-1"],
    )
    store.write_meta(crashed)
    store.append_message(crashed.id, "user", "unfinished")

    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    restored = await manager.get(crashed.id)
    assert restored.status == "interrupted"
    assert restored.interrupted_reason == "daemon_restarted"
    assert await manager.get_history(crashed.id) == [
        {"role": "user", "content": "unfinished"}
    ]
    assert store.read_meta(crashed.id).status == "interrupted"


async def test_manager_migrates_legacy_session_to_current_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    workspace.mkdir()
    store = SessionStore(tmp_path / "sessions")
    legacy = Session(
        id="sess-legacy",
        mode="chat",
        status="waiting_for_input",
        title="legacy",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.write_meta(legacy)
    monkeypatch.chdir(workspace)

    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    restored = await manager.get(legacy.id)
    assert restored.workspace == str(workspace.resolve())
    assert store.read_meta(legacy.id).workspace == str(workspace.resolve())


async def test_resume_reopens_closed_chat_and_keeps_history(tmp_path: Path) -> None:
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), bus)  # type: ignore[arg-type]
    session = await manager.create("chat", "old chat")
    store.append_message(session.id, "user", "remember this")
    await manager.close(session.id)

    resumed = await manager.resume(session.id)

    assert resumed.status == "waiting_for_input"
    assert await manager.get_history(session.id) == [
        {"role": "user", "content": "remember this"}
    ]
    assert events[-1].type == "session.resumed"  # type: ignore[attr-defined]


async def test_one_shot_session_cannot_be_resumed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]
    session = await manager.create("one_shot")

    with pytest.raises(HandlerError) as exc:
        await manager.resume(session.id)

    assert exc.value.code == SESSION_NOT_RESUMABLE


async def test_list_sessions_is_newest_first_and_filterable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    old = Session(
        id="sess-old",
        mode="chat",
        status="closed",
        title="old",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    new = Session(
        id="sess-new",
        mode="chat",
        status="waiting_for_input",
        title="new",
        created_at="2026-01-02T00:00:00+00:00",
        updated_at="2026-01-02T00:00:00+00:00",
    )
    store.write_meta(old)
    store.write_meta(new)
    manager = SessionManager(store, lambda: _Runner(), EventBus())  # type: ignore[arg-type]

    assert [item.id for item in await manager.list_sessions()] == ["sess-new", "sess-old"]
    assert [item.id for item in await manager.list_sessions(status="closed")] == ["sess-old"]
