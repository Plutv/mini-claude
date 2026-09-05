from __future__ import annotations

import json

import httpx
import pytest

from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.openai_provider import OpenAICompatibleProvider


async def test_openai_compatible_allows_unauthenticated_endpoint() -> None:
    provider = OpenAICompatibleProvider(
        "local-model",
        api_key_env="",
        base_url="http://127.0.0.1:11434/v1",
    )

    # 客户端延迟创建：实例化时不校验、不建连接
    assert provider._client is None
    provider._ensure_client()
    assert "Authorization" not in provider._client.headers
    await provider.close()


def test_openai_compatible_still_requires_configured_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_PROVIDER_KEY", raising=False)

    provider = OpenAICompatibleProvider(
        "remote-model",
        api_key_env="MISSING_PROVIDER_KEY",
        base_url="https://example.test/v1",
    )
    # 延迟到首次 chat 才检查 key
    with pytest.raises(SystemExit, match="MISSING_PROVIDER_KEY not set"):
        provider._ensure_client()


async def test_openai_compatible_stream_accumulates_text_tool_calls_and_usage() -> None:
    chunks = [
        {"choices": [{"delta": {"content": "hello "}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {"name": "read_file", "arguments": '{"pa'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'th":"README.md"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
    body += "data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(
        base_url="https://example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    provider = OpenAICompatibleProvider("demo", client=client, context_window=100)
    events: list[object] = []
    bus = EventBus()

    async def collect(event: object) -> None:
        events.append(event)

    bus.subscribe(collect)  # type: ignore[arg-type]
    result = await provider.chat(
        [{"role": "user", "content": "inspect"}],
        [
            {
                "name": "read_file",
                "description": "read",
                "input_schema": {"type": "object"},
            }
        ],
        bus,
        "run",
    )
    await client.aclose()

    assert result.stop_reason == "tool_use"
    assert result.text == "hello "
    assert result.tool_calls[0].name == "read_file"
    assert result.tool_calls[0].input == {"path": "README.md"}
    assert result.usage is not None
    assert result.usage.context_pct == 0.25
    assert [getattr(event, "type", "") for event in events] == [
        "llm.model_selected",
        "llm.token",
        "llm.usage",
    ]
