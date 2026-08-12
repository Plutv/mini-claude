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
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception:
            if attempt == attempts - 1:
                raise
            await sleep(2**attempt)
    raise RuntimeError("unreachable")
