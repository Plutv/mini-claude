import pytest

from app.retry import TransientError, call_with_retry


@pytest.mark.asyncio
async def test_programming_error_is_never_retried() -> None:
    calls = 0

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise ValueError("bad input")

    async def no_sleep(delay: float) -> None:
        return None

    with pytest.raises(ValueError):
        await call_with_retry(operation, sleep=no_sleep)
    assert calls == 1


@pytest.mark.asyncio
async def test_attempts_must_be_positive() -> None:
    async def operation() -> str:
        return "ok"

    with pytest.raises(ValueError):
        await call_with_retry(operation, attempts=0)


@pytest.mark.asyncio
async def test_last_transient_error_is_propagated_after_budget_exhausted() -> None:
    calls = 0
    delays = []

    async def operation() -> str:
        nonlocal calls
        calls += 1
        raise TransientError("still down")

    async def sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(TransientError, match="still down"):
        await call_with_retry(operation, attempts=3, sleep=sleep)
    assert calls == 3
    assert delays == [1, 2]
