import pytest

from app.router import DuplicateRequestError, MessageRouter


@pytest.mark.asyncio
async def test_unknown_and_duplicate_responses_are_ignored() -> None:
    async def on_event(message) -> None:
        raise AssertionError("not an event")

    router = MessageRouter(on_event)
    future = router.register("known")
    await router.dispatch({"id": "unknown", "result": "wrong"})
    assert not future.done()
    await router.dispatch({"id": "known", "result": "right"})
    assert (await future)["result"] == "right"
    await router.dispatch({"id": "known", "result": "duplicate"})
    assert router.pending_count() == 0


@pytest.mark.asyncio
async def test_duplicate_pending_request_id_is_rejected() -> None:
    async def on_event(message) -> None:
        return None

    router = MessageRouter(on_event)
    router.register("same")
    with pytest.raises(DuplicateRequestError):
        router.register("same")
