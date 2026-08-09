from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from kama_claude.core.config import McpServerConfig
from kama_claude.core.mcp.client import McpServerUnavailableError, McpToolDef
from kama_claude.core.mcp.server import McpServerManager, McpServerRuntime


class _FakeClient:
    def __init__(self, *, call_timeout_s: float = 30.0) -> None:
        del call_timeout_s
        self.connected = False

    async def connect_tcp(self, host: str, port: int) -> None:
        await asyncio.sleep(0.05)
        self.connected = True

    async def connect_stdio(self, command: str, args: list[str], env: object) -> None:
        await asyncio.sleep(0.05)
        self.connected = True

    async def list_tools(self) -> list[McpToolDef]:
        return [McpToolDef(name="read", description="read", read_only_hint=True)]

    async def call_tool(self, name: str, arguments: dict[str, object]) -> str:
        return "ok"

    async def close(self) -> None:
        self.connected = False


async def test_manager_starts_servers_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kama_claude.core.mcp.server.McpClient", _FakeClient)
    manager = McpServerManager()
    configs = [
        McpServerConfig(name="one", transport="tcp", port=1),
        McpServerConfig(name="two", transport="tcp", port=2),
    ]
    loop = asyncio.get_running_loop()
    started = loop.time()

    await manager.start_all(configs)

    assert loop.time() - started < 0.09
    assert len(manager.get_tools()) == 2
    assert all(item["status"] == "healthy" for item in manager.health())
    await manager.stop_all()


async def test_runtime_recovers_connection_without_replaying_failed_call() -> None:
    runtime = McpServerRuntime(McpServerConfig(name="demo", transport="tcp"))
    failed_client = AsyncMock()
    failed_client.connected = True
    failed_client.call_tool.side_effect = McpServerUnavailableError("connection lost")
    failed_client.close = AsyncMock()
    runtime._client = failed_client
    runtime.start = AsyncMock(return_value=[])

    with pytest.raises(McpServerUnavailableError, match="was not replayed"):
        await runtime.call_tool("write", {"value": 1})

    failed_client.call_tool.assert_awaited_once()
    failed_client.close.assert_awaited_once()
    runtime.start.assert_awaited_once()
    assert runtime.restart_count == 1
