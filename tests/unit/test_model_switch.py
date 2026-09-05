"""覆盖运行时模型切换：ProviderRouter 侧能力 + /model 斜杠命令侧行为。

分两层测：
- router 层：switch_model / current_model / list_models / model_label，以及
  "显式切换后不再静默 fallback" 这条关键语义。
- session 层：/model 不启动 agent run、只回一条 notice 并广播 session.notice。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.router import ProviderRouter, UnknownModelError
from kama_claude.core.llm.types import LlmResponse, UsageStats
from kama_claude.core.runner import RunOutcome
from kama_claude.core.session.manager import SessionManager
from kama_claude.core.session.store import SessionStore


class _FakeProvider:
    def __init__(self, model: str, result: str = "ok", *, error: Exception | None = None):
        self.model_name = model
        self.result = result
        self.error = error
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        **kwargs: Any,
    ) -> LlmResponse:
        del messages, tool_schemas, bus, run_id, kwargs
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LlmResponse(stop_reason="end_turn", text=self.result, usage=UsageStats(1, 1))


def _router() -> tuple[ProviderRouter, _FakeProvider, _FakeProvider]:
    primary = _FakeProvider("claude-sonnet-4-6", "from anthropic")
    fallback = _FakeProvider("qwen3.8:27b", "from ollama")
    router = ProviderRouter(
        {"anthropic": primary, "ollama": fallback},
        default_provider="anthropic",
        fallback_providers=["ollama"],
    )
    return router, primary, fallback


class _Runner:
    # 记录是否真的启动了 agent run；/model 不应该走到这里
    def __init__(self) -> None:
        self.calls = 0

    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Any = None,
        store: SessionStore | None = None,
        system_prompt_override: str | None = None,
        tool_whitelist: list[str] | None = None,
    ) -> RunOutcome:
        del goal, run_id, session, store, system_prompt_override, tool_whitelist
        self.calls += 1
        return RunOutcome(status="success", result="done", reason=None)


# ---------------------------------------------------------------------------
# ProviderRouter：切换能力
# ---------------------------------------------------------------------------

# 功能：list_models 返回全部 provider 名，current_model 默认等于配置里的 default
# 设计：直接构造 router，验证元信息接口
def test_router_exposes_model_metadata() -> None:
    router, _primary, _fallback = _router()

    assert router.list_models() == ["anthropic", "ollama"]
    assert router.current_model() == "anthropic"
    assert router.model_label("ollama") == "qwen3.8:27b"


# 功能：switch_model 改变后续请求走哪个 provider
# 设计：切到 ollama 后再 chat，确认命中的是 ollama 的 fake
async def test_switch_model_changes_routing() -> None:
    router, primary, fallback = _router()

    router.switch_model("ollama")
    result = await router.chat([], [], EventBus(), "run")

    assert router.current_model() == "ollama"
    assert result.text == "from ollama"
    assert primary.calls == 0
    assert fallback.calls == 1


# 功能：切到未知模型抛 UnknownModelError，并把可用列表写进报错信息
# 设计：断言异常类型与提示文本，保证 /model 能把原文回显给用户
def test_switch_model_rejects_unknown_name() -> None:
    router, primary, _fallback = _router()

    with pytest.raises(UnknownModelError) as excinfo:
        router.switch_model("gpt-5")

    assert "available" in str(excinfo.value)
    assert "anthropic" in str(excinfo.value)
    assert router.current_model() == "anthropic"
    assert primary.calls == 0


# 功能：/model auto 恢复为配置默认，且不改变配置本身
# 设计：先切到 ollama 再 auto，确认回到 anthropic
def test_switch_model_auto_restores_configured_default() -> None:
    router, _primary, _fallback = _router()

    router.switch_model("ollama")
    router.switch_model("auto")

    assert router.current_model() == "anthropic"


# 功能：显式切换后主 provider 出错不再静默 fallback
# 设计：这是关键的语义差异——用户指名要 ollama，就应该是 ollama 报错而不是偷偷用 anthropic
async def test_pinned_model_does_not_fall_back() -> None:
    primary = _FakeProvider("claude-sonnet-4-6")
    broken = _FakeProvider("qwen3.8:27b", error=RuntimeError("connection refused"))
    router = ProviderRouter(
        {"anthropic": primary, "ollama": broken},
        default_provider="anthropic",
        fallback_providers=["ollama"],
    )

    router.switch_model("ollama")
    with pytest.raises(RuntimeError, match="connection refused"):
        await router.chat([], [], EventBus(), "run")

    assert primary.calls == 0


# 功能：未显式切换（auto）时保留原有 fallback 行为
# 设计：与上一个测试对照，确认 pinning 没有破坏默认容错
async def test_unpinned_model_still_falls_back() -> None:
    broken = _FakeProvider("claude-sonnet-4-6", error=RuntimeError("overloaded"))
    fallback = _FakeProvider("qwen3.8:27b", "from ollama")
    router = ProviderRouter(
        {"anthropic": broken, "ollama": fallback},
        default_provider="anthropic",
        fallback_providers=["ollama"],
    )

    result = await router.chat([], [], EventBus(), "run")

    assert result.text == "from ollama"


# ---------------------------------------------------------------------------
# /model 斜杠命令
# ---------------------------------------------------------------------------

async def _new_session(tmp_path: Path, switcher: Any) -> tuple[SessionManager, Any, list[Any]]:
    events: list[Any] = []
    bus = EventBus()

    async def collect(event: Any) -> None:
        events.append(event)

    bus.subscribe(collect)
    runner = _Runner()
    manager = SessionManager(
        SessionStore(tmp_path),
        lambda: runner,
        bus,
        model_switcher=switcher,
    )
    session = await manager.create("chat", "title")
    return manager, session, events


# 功能：/model 无参列出当前模型与所有可选项
# 设计：断言不启动 run、返回空串、notice 文本含 current 与两个 provider
async def test_model_command_lists_options(tmp_path: Path) -> None:
    router, _primary, _fallback = _router()
    manager, session, events = await _new_session(tmp_path, router)

    result = await manager.send_message(session.id, "/model")

    assert result == ""
    notices = [e for e in events if e.type == "session.notice"]
    assert len(notices) == 1
    assert "current: anthropic" in notices[0].message
    assert "ollama" in notices[0].message
    assert "auto" in notices[0].message
    # 控制命令不应触发 agent run
    assert [e.type for e in events].count("session.resumed") == 0


# 功能：/model <name> 真正切换 provider
# 设计：切到 ollama 后断言 router.current_model 与 notice 文本
async def test_model_command_switches_provider(tmp_path: Path) -> None:
    router, _primary, _fallback = _router()
    manager, session, events = await _new_session(tmp_path, router)

    await manager.send_message(session.id, "/model ollama")

    assert router.current_model() == "ollama"
    notice = [e for e in events if e.type == "session.notice"][0]
    assert "ollama" in notice.message


# 功能：/model auto 恢复配置默认
# 设计：先切 ollama 再 auto
async def test_model_command_auto_restores_default(tmp_path: Path) -> None:
    router, _primary, _fallback = _router()
    manager, session, events = await _new_session(tmp_path, router)

    await manager.send_message(session.id, "/model ollama")
    await manager.send_message(session.id, "/model auto")

    assert router.current_model() == "anthropic"
    notices = [e for e in events if e.type == "session.notice"]
    assert "auto" in notices[-1].message


# 功能：未知模型名不抛异常，而是把错误原文作为 notice 回显
# 设计：断言 switch 未生效且 events 里是 notice 而非异常
async def test_model_command_unknown_name_returns_error_notice(tmp_path: Path) -> None:
    router, _primary, _fallback = _router()
    manager, session, events = await _new_session(tmp_path, router)

    await manager.send_message(session.id, "/model gpt-5")

    assert router.current_model() == "anthropic"
    notice = [e for e in events if e.type == "session.notice"][0]
    assert "unknown model" in notice.message


# 功能：单 provider（无 router）时 /model 给出解释性提示，而不是崩掉
# 设计：model_switcher=None，断言提示文本引导用户配置 providers
async def test_model_command_without_router_explains(tmp_path: Path) -> None:
    manager, session, events = await _new_session(tmp_path, None)

    await manager.send_message(session.id, "/model")

    notice = [e for e in events if e.type == "session.notice"][0]
    assert "unavailable" in notice.message


# 功能：/model 之后会话回到可输入状态，且控制命令被持久化到 thread
# 设计：断言 session.waiting_for_input 事件与 thread 中留下两条消息
async def test_model_command_persists_and_returns_to_waiting(tmp_path: Path) -> None:
    router, _primary, _fallback = _router()
    manager, session, events = await _new_session(tmp_path, router)

    await manager.send_message(session.id, "/model ollama")

    types = [e.type for e in events]
    assert "session.waiting_for_input" in types
    messages = manager._store.read_messages(session.id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
