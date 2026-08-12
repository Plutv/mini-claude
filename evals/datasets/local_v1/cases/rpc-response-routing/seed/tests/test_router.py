import pytest

from app.router import MessageRouter


@pytest.mark.asyncio
async def test_out_of_order_responses_recover_correct_futures() -> None:
    events = []

    async def on_event(message) -> None:
        events.append(message)

    router = MessageRouter(on_event)
    first = router.register("1")
    second = router.register("2")
    await router.dispatch({"id": "2", "result": "second"})
    await router.dispatch({"id": "1", "result": "first"})
    assert (await first)["result"] == "first"
    assert (await second)["result"] == "second"
    assert router.pending_count() == 0


@pytest.mark.asyncio
async def test_event_is_delivered_to_callback() -> None:
    events = []

    async def on_event(message) -> None:
        events.append(message)

    router = MessageRouter(on_event)
    await router.dispatch({"type": "event", "topic": "llm.token", "data": "x"})
    assert events == [{"type": "event", "topic": "llm.token", "data": "x"}]
