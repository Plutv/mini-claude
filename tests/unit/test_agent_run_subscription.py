from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kama_claude.core.app as app_module
from kama_claude.core.app import CoreApp
from kama_claude.core.transport.ipc_broadcaster import IpcEventBroadcaster


@pytest.mark.asyncio
async def test_agent_run_subscribes_connection_before_background_run(monkeypatch) -> None:
    app = CoreApp()
    app._sessions = MagicMock()
    app._sessions.create = AsyncMock(return_value=SimpleNamespace(id="session-1"))
    app._sessions.send_message = AsyncMock(return_value="run-unused")
    app._broadcaster = IpcEventBroadcaster()
    connection_writer = MagicMock()
    monkeypatch.setattr(app_module, "get_connection_writer", lambda: connection_writer)

    result = await app._agent_run_handler(
        {"goal": "inspect repo", "subscribe_topics": ["run.*", "tool.*"]}
    )
    tasks = list(app._running_runs)
    assert len(tasks) == 1
    await tasks[0]

    assert result.subscription_id is not None
    assert len(app._broadcaster._subscriptions) == 1
    subscription = app._broadcaster._subscriptions[0]
    assert subscription.writer is connection_writer
    assert subscription.scope == f"run:{result.run_id}"
    app._sessions.send_message.assert_awaited_once_with(
        "session-1", "inspect repo", run_id=result.run_id
    )
