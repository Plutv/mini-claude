from __future__ import annotations

from typing import Any

import pytest

from kama_claude.core.bus.events import LlmTokenEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.router import ProviderRouter
from kama_claude.core.llm.types import LlmResponse, UsageStats


class _FakeProvider:
    def __init__(self, result: str = "ok", *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = 0

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        **kwargs: Any,
    ) -> LlmResponse:
        del messages, tool_schemas, bus, run_id, kwargs
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LlmResponse(
            stop_reason="end_turn",
            text=self.result,
            usage=UsageStats(1, 1),
        )


class _PartialFailureProvider(_FakeProvider):
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        **kwargs: Any,
    ) -> LlmResponse:
        del messages, tool_schemas, kwargs
        self.calls += 1
        await bus.publish(LlmTokenEvent(run_id=run_id, token="partial", ts="now"))
        raise RuntimeError("stream dropped")


async def test_router_falls_back_before_any_token_is_visible() -> None:
    primary = _FakeProvider(error=RuntimeError("offline"))
    fallback = _FakeProvider("fallback answer")
    router = ProviderRouter(
        {"primary": primary, "fallback": fallback},
        default_provider="primary",
        fallback_providers=["fallback"],
    )

    result = await router.chat([], [], EventBus(), "run")

    assert result.text == "fallback answer"
    assert primary.calls == 1
    assert fallback.calls == 1


async def test_router_never_falls_back_after_partial_stream_output() -> None:
    primary = _PartialFailureProvider()
    fallback = _FakeProvider("duplicate answer")
    router = ProviderRouter(
        {"primary": primary, "fallback": fallback},
        default_provider="primary",
        fallback_providers=["fallback"],
    )

    with pytest.raises(RuntimeError, match="stream dropped"):
        await router.chat([], [], EventBus(), "run")

    assert fallback.calls == 0


async def test_rule_router_selects_complex_provider() -> None:
    cheap = _FakeProvider("cheap")
    complex_provider = _FakeProvider("complex")
    router = ProviderRouter(
        {"cheap": cheap, "complex": complex_provider},
        default_provider="cheap",
        strategy="rule_based",
        complex_provider="complex",
    )

    result = await router.chat(
        [{"role": "user", "content": str(index)} for index in range(12)],
        [],
        EventBus(),
        "run",
    )

    assert result.text == "complex"
    assert cheap.calls == 0
    assert complex_provider.calls == 1
