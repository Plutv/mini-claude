import pytest

from app.retry import TransientError, call_with_retry


@pytest.mark.asyncio
async def test_transient_idempotent_operation_is_retried() -> None:
    calls = 0
    delays = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise TransientError("temporary")
        return "ok"

    async def sleep(delay: float) -> None:
        delays.append(delay)

    assert await call_with_retry(operation, sleep=sleep) == "ok"
    assert calls == 3
    assert delays == [1, 2]


@pytest.mark.asyncio
async def test_non_idempotent_operation_is_not_retried() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise TransientError("temporary")

    async def no_sleep(delay: float) -> None:
        return None

    with pytest.raises(TransientError):
        await call_with_retry(operation, idempotent=False, sleep=no_sleep)
    assert calls == 1
