from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")


class TransientError(RuntimeError):
    pass


async def call_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    idempotent: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(attempts):
        try:
            return await operation()
        except TransientError:
            if not idempotent or attempt == attempts - 1:
                raise
            await sleep(2**attempt)
    raise RuntimeError("unreachable")
