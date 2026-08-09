from __future__ import annotations

import time
from dataclasses import dataclass

from pydantic import BaseModel

from kama_claude.core.bus.events import LlmTokenEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.types import LlmResponse


class _ForwardingBus(EventBus):
    def __init__(self, target: EventBus) -> None:
        super().__init__()
        self._target = target
        self.token_count = 0

    async def publish(self, event: BaseModel) -> None:
        if isinstance(event, LlmTokenEvent):
            self.token_count += 1
        await self._target.publish(event)


@dataclass
class _Health:
    failures: int = 0
    open_until: float = 0.0


class ProviderRouter:
    """Select providers and fail over only before streamed output becomes visible."""

    def __init__(
        self,
        providers: dict[str, LLMProvider],
        *,
        default_provider: str,
        fallback_providers: list[str] | None = None,
        strategy: str = "static",
        complex_provider: str | None = None,
        failure_threshold: int = 2,
        cooldown_s: float = 30.0,
    ) -> None:
        if default_provider not in providers:
            raise ValueError(f"unknown default provider: {default_provider}")
        self._providers = providers
        self._default = default_provider
        self._fallbacks = fallback_providers or []
        self._strategy = strategy
        self._complex_provider = complex_provider
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._health = {name: _Health() for name in providers}

    def _primary(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
    ) -> str:
        if (
            self._strategy == "rule_based"
            and self._complex_provider in self._providers
            and (len(messages) >= 12 or len(tool_schemas) >= 10)
        ):
            return str(self._complex_provider)
        return self._default

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        primary = self._primary(messages, tool_schemas)
        candidates = list(dict.fromkeys([primary, self._default, *self._fallbacks]))
        last_error: Exception | None = None
        now = time.monotonic()
        for name in candidates:
            provider = self._providers.get(name)
            if provider is None or self._health[name].open_until > now:
                continue
            forwarding_bus = _ForwardingBus(bus)
            try:
                result = await provider.chat(
                    messages,
                    tool_schemas,
                    forwarding_bus,
                    run_id,
                    step=step,
                    system=system,
                )
                self._health[name] = _Health()
                return result
            except Exception as exc:
                last_error = exc
                health = self._health[name]
                health.failures += 1
                if health.failures >= self._failure_threshold:
                    health.open_until = time.monotonic() + self._cooldown_s
                if forwarding_bus.token_count:
                    raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("no healthy LLM provider is available")

    async def close(self) -> None:
        for provider in self._providers.values():
            close = getattr(provider, "close", None)
            if close is not None:
                await close()
