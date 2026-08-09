from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kama_claude.core.mcp.client import (
    McpCallTimeoutError,
    McpClient,
    McpServerUnavailableError,
)


class _Writer:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def write(self, data: bytes) -> None:
        self.messages.append(json.loads(data))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None

    async def wait_closed(self) -> None:
        return None


async def _connected_client(timeout: float = 1.0) -> tuple[McpClient, asyncio.StreamReader, _Writer]:
    client = McpClient(call_timeout_s=timeout)
    reader = asyncio.StreamReader()
    writer = _Writer()
    client._reader = reader
    client._tcp_writer = writer  # type: ignore[assignment]
    client._transport = "tcp"
    client._reader_task = asyncio.create_task(client._reader_loop())
    return client, reader, writer


async def test_concurrent_calls_are_routed_by_response_id() -> None:
    client, reader, writer = await _connected_client()
    first = asyncio.create_task(client._call("first", {}))
    second = asyncio.create_task(client._call("second", {}))
    await asyncio.sleep(0)

    assert [message["method"] for message in writer.messages] == ["first", "second"]
    reader.feed_data(b'{"jsonrpc":"2.0","id":2,"result":{"value":"B"}}\n')
    reader.feed_data(b'{"jsonrpc":"2.0","id":1,"result":{"value":"A"}}\n')

    assert await first == {"value": "A"}
    assert await second == {"value": "B"}
    await client.close()


async def test_call_timeout_sends_cancellation_notification() -> None:
    client, _reader, writer = await _connected_client(timeout=0.01)

    with pytest.raises(McpCallTimeoutError):
        await client._call("slow", {})

    assert writer.messages[-1] == {
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 1},
    }
    await client.close()


async def test_connection_eof_fails_all_pending_calls() -> None:
    client, reader, _writer = await _connected_client()
    pending = asyncio.create_task(client._call("work", {}))
    await asyncio.sleep(0)
    reader.feed_eof()

    with pytest.raises(McpServerUnavailableError, match="closed connection"):
        await pending
    await client.close()
