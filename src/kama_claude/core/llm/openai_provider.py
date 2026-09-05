from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx

from kama_claude.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from kama_claude.core.events.bus import EventBus
from kama_claude.core.llm.types import LlmResponse, ToolCallBlock, UsageStats


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _convert_messages(messages: list[dict[str, object]]) -> list[dict[str, object]]:
    converted: list[dict[str, object]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            converted.append({"role": role, "content": str(content)})
            continue
        if role == "assistant":
            texts: list[str] = []
            calls: list[dict[str, object]] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
                elif block.get("type") == "tool_use":
                    calls.append(
                        {
                            "id": str(block.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": str(block.get("name", "")),
                                "arguments": json.dumps(
                                    block.get("input", {}), ensure_ascii=False
                                ),
                            },
                        }
                    )
            row: dict[str, object] = {"role": "assistant", "content": "".join(texts)}
            if calls:
                row["tool_calls"] = calls
            converted.append(row)
            continue
        text_blocks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                converted.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id", "")),
                        "content": str(block.get("content", "")),
                    }
                )
            elif block.get("type") == "text":
                text_blocks.append(str(block.get("text", "")))
        if text_blocks:
            converted.append({"role": "user", "content": "".join(text_blocks)})
    return converted


class OpenAICompatibleProvider:
    """Streaming adapter for OpenAI-compatible chat-completions endpoints."""

    def __init__(
        self,
        model: str,
        *,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        context_window: int = 128_000,
        client: httpx.AsyncClient | Any | None = None,
    ) -> None:
        self._model = model
        self._context_window = context_window
        self._api_key_env = api_key_env
        self._base_url = base_url
        self._client: httpx.AsyncClient | Any | None = client
        self._owns_client = client is None

    # 首次调用时才创建 httpx 客户端（并据此检查 key）。ollama 这类
    # api_key_env="" 的 endpoint 不需要 key，因此即使没有 OPENAI_API_KEY 也能启动。
    def _ensure_client(self) -> httpx.AsyncClient | Any:
        if self._client is not None:
            return self._client
        headers: dict[str, str] = {}
        if self._api_key_env:
            api_key = os.environ.get(self._api_key_env)
            if not api_key:
                raise SystemExit(f"{self._api_key_env} not set")
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=20.0),
        )
        return self._client

    # 供 /model 列表展示实际模型名
    @property
    def model_name(self) -> str:
        return self._model

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
        del step
        client = self._ensure_client()
        await bus.publish(
            LlmModelSelectedEvent(
                run_id=run_id, model=self._model, strategy="provider", ts=_now()
            )
        )
        rendered = _convert_messages(messages)
        if system:
            rendered.insert(0, {"role": "system", "content": system})
        payload: dict[str, object] = {
            "model": self._model,
            "messages": rendered,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tool_schemas:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["input_schema"],
                    },
                }
                for tool in tool_schemas
            ]

        text_parts: list[str] = []
        partial_calls: dict[int, dict[str, str]] = {}
        finish_reason = "stop"
        usage_raw: dict[str, int] = {}
        async with client.stream(
            "POST", "/chat/completions", json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if isinstance(chunk.get("usage"), dict):
                    usage_raw = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.get("finish_reason") or finish_reason
                delta = choice.get("delta") or {}
                token = delta.get("content")
                if token:
                    text_parts.append(str(token))
                    await bus.publish(
                        LlmTokenEvent(run_id=run_id, token=str(token), ts=_now())
                    )
                for call in delta.get("tool_calls") or []:
                    index = int(call.get("index", 0))
                    partial = partial_calls.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    if call.get("id"):
                        partial["id"] += str(call["id"])
                    function = call.get("function") or {}
                    partial["name"] += str(function.get("name") or "")
                    partial["arguments"] += str(function.get("arguments") or "")

        prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
        completion_tokens = int(usage_raw.get("completion_tokens", 0))
        context_pct = (prompt_tokens + completion_tokens) / self._context_window
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                context_pct=context_pct,
                ts=_now(),
            )
        )
        tool_calls: list[ToolCallBlock] = []
        for partial in partial_calls.values():
            try:
                arguments = json.loads(partial["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {"_raw_arguments": partial["arguments"]}
            tool_calls.append(
                ToolCallBlock(
                    id=partial["id"], name=partial["name"], input=arguments
                )
            )
        stop_reason = "tool_use" if finish_reason == "tool_calls" else "end_turn"
        if finish_reason == "length":
            stop_reason = "max_tokens"
        return LlmResponse(
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            text="".join(text_parts),
            usage=UsageStats(prompt_tokens, completion_tokens, context_pct=context_pct),
        )

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
