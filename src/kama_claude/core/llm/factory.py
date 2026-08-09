from __future__ import annotations

from kama_claude.core.config import LlmConfig
from kama_claude.core.llm.base import LLMProvider
from kama_claude.core.llm.openai_provider import OpenAICompatibleProvider
from kama_claude.core.llm.provider import AnthropicProvider
from kama_claude.core.llm.router import ProviderRouter


def build_provider(config: LlmConfig) -> LLMProvider:
    if not config.providers:
        return AnthropicProvider(config.default_model)

    providers: dict[str, LLMProvider] = {}
    for spec in config.providers:
        if spec.name in providers:
            raise SystemExit(f"duplicate LLM provider name: {spec.name}")
        if spec.kind == "anthropic":
            providers[spec.name] = AnthropicProvider(
                spec.model,
                api_key_env=spec.api_key_env,
                base_url=spec.base_url,
                context_window=spec.context_window,
            )
        else:
            providers[spec.name] = OpenAICompatibleProvider(
                spec.model,
                api_key_env=spec.api_key_env,
                base_url=spec.base_url,
                context_window=spec.context_window,
            )
    return ProviderRouter(
        providers,
        default_provider=config.default_provider,
        fallback_providers=config.fallback_providers,
        strategy=config.router,
        complex_provider=config.complex_provider or None,
    )
