from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


class DuplicateRequestError(RuntimeError):
    pass


_PENDING: dict[str, asyncio.Task[str]] = {}


async def run_pending(
    key: str,
    operation: Callable[[], Awaitable[str]],
    timeout: float,
) -> str:
    task = asyncio.create_task(operation())
    _PENDING[key] = task
    return await asyncio.wait_for(task, timeout=timeout)


def pending_count() -> int:
    return len(_PENDING)
