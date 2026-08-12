import asyncio

import pytest

from app.pending import DuplicateRequestError, pending_count, run_pending


@pytest.mark.asyncio
async def test_duplicate_key_does_not_replace_original_request() -> None:
    release = asyncio.Event()

    async def first_operation() -> str:
        await release.wait()
        return "first"

    first = asyncio.create_task(run_pending("same", first_operation, 1))
    await asyncio.sleep(0)
    with pytest.raises(DuplicateRequestError):
        await run_pending("same", lambda: asyncio.sleep(0, result="second"), 1)
    release.set()
    assert await first == "first"
    assert pending_count() == 0


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_and_cleans_registry() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def operation() -> str:
        try:
            started.set()
            await asyncio.sleep(10)
        finally:
            stopped.set()
        return "late"

    outer = asyncio.create_task(run_pending("cancel", operation, 20))
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert stopped.is_set()
    assert pending_count() == 0
