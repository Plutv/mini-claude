from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class DuplicateRequestError(RuntimeError):
    pass


class MessageRouter:
    def __init__(self, on_event: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._on_event = on_event
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def register(self, request_id: str) -> asyncio.Future[dict[str, Any]]:
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    async def dispatch(self, message: dict[str, Any]) -> None:
        if message.get("type") == "event":
            await self._on_event(message)
            return
        if self._pending:
            request_id = next(iter(self._pending))
            future = self._pending.pop(request_id)
            future.set_result(message)

    def pending_count(self) -> int:
        return len(self._pending)
