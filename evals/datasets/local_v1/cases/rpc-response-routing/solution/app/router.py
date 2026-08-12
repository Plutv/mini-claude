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
        existing = self._pending.get(request_id)
        if existing is not None and not existing.done():
            raise DuplicateRequestError(f"request already pending: {request_id}")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        return future

    async def dispatch(self, message: dict[str, Any]) -> None:
        if message.get("type") == "event":
            await self._on_event(message)
            return
        request_id = str(message.get("id", ""))
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(message)

    def pending_count(self) -> int:
        return len(self._pending)
