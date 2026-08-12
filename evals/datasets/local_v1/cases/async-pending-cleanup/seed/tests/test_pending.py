import asyncio

import pytest

from app.pending import pending_count, run_pending


@pytest.mark.asyncio
async def test_success_cleans_registry() -> None:
    async def operation() -> str:
        return "ok"

    assert await run_pending("success", operation, 1) == "ok"
    assert pending_count() == 0


@pytest.mark.asyncio
async def test_timeout_cleans_registry_and_cancels_operation() -> None:
    cancelled = asyncio.Event()

    async def operation() -> str:
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()
        return "late"

    with pytest.raises(TimeoutError):
        await run_pending("timeout", operation, 0.01)
    assert cancelled.is_set()
    assert pending_count() == 0
