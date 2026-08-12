from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress


class DuplicateRequestError(RuntimeError):
    pass


_PENDING: dict[str, asyncio.Task[str]] = {}


async def run_pending(
    key: str,
    operation: Callable[[], Awaitable[str]],
    timeout: float,
) -> str:
    if key in _PENDING:
        raise DuplicateRequestError(f"request already pending: {key}")
    task = asyncio.create_task(operation())
    _PENDING[key] = task
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        _PENDING.pop(key, None)


def pending_count() -> int:
    return len(_PENDING)
