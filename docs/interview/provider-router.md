# Provider 与模型路由面试卡 — 简历第四点

> 对应代码：`src/kama_claude/core/llm/base.py`、`core/llm/provider.py`（Anthropic）、`core/llm/openai_provider.py`、`core/llm/router.py`、`core/trace/provider.py`。  
> 真实实验参考：`tests/unit/test_llm_provider.py`、`test_openai_provider.py`、`test_provider_router.py`、`test_tracing_provider.py`。

---

## 1. 解决的问题
- 需要同时支持 Anthropic 和 OpenAI-compatible API，但两者的消息格式、工具调用协议、流式响应完全不同。
- 主模型网络异常时不能简单切备用模型——流式输出已经开始时切换会造成用户可见的拼接残缺。
- 需要记录每次 LLM 调用的延迟、token 消耗、工具调用详情，用于后续评测和调试。

## 2. 最初方案
直接调用 Anthropic SDK，没有抽象层；模型异常时在外层 try/catch 重试。

## 3. 暴露的问题
- 写死 Anthropic API → 无法接入 OpenAI 或自托管模型。
- 重试时如果已经流式输出了一部分文本，备用模型的回答会拼接在后面——用户体验极差。
- 没有调用记录，出了问题只能靠日志猜。

## 4. 最终设计

### 统一接口：LLMProvider Protocol
```python
class LLMProvider(Protocol):
    async def chat(
        self, messages, tool_schemas, bus, run_id, *, step=0, system=None
    ) -> LlmResponse
```

- 所有 Provider 返回统一的 `LlmResponse`（`stop_reason`、`tool_calls`、`text`、`usage`、`thinking_blocks`）。
- `bus` 参数用于发布 `LlmTokenEvent`（流式 token）和 `LlmUsageEvent`（最终用量）。

### AnthropicProvider（`provider.py:72`）
- 使用 `anthropic.AsyncAnthropic.messages.stream()` 流式调用。
- 流式中逐 token 发布 `LlmTokenEvent`（但**重试时不重复发布**，`attempt == 1` 才发）。
- 网络层异常（`httpx.RemoteProtocolError`、`httpx.ReadError`、`httpx.ConnectError`）自动重试 3 次，退避 1s→2s→4s。
- **prompt caching**：system prompt 和最后一个 tool schema 加 `cache_control: ephemeral`；最后一条消息的最后一条非-thinking block 也加 cache marker——不污染持久化历史（`_with_message_cache_breakpoint`）。
- `thinking_blocks` 完整保留 signature，后续请求必须原样传回（extended thinking 模式）。
- context_pct 计算：`input_tokens + cache_read + cache_create + output_tokens` / context_window。

### OpenAICompatibleProvider（`openai_provider.py:75`）
- 使用 `httpx.AsyncClient.stream()` 直接消费 SSE stream。
- **_convert_messages**：将 Anthropic 格式的 messages 转换为 OpenAI 格式：
  - assistant 的 `text` + `tool_use` block → 合并为 `{"role": "assistant", "content": text, "tool_calls": [...]}`
  - user 的 `tool_result` block → 拆成多条 `{"role": "tool", "tool_call_id": ..., "content": ...}`
- **流式 tool_calls 增量拼接**：OpenAI 的 tool_calls 在 stream 中分片到达（`index` 标识），用 `partial_calls: dict[int, dict]` 累加 `id`/`name`/`arguments`，最后 `json.loads` 解析参数。
- `finish_reason == "tool_calls"` → `stop_reason = "tool_use"`；`finish_reason == "length"` → `stop_reason = "max_tokens"`。
- 没有 prompt caching（OpenAI 不支持 ephemeral cache control）。

### ProviderRouter（`router.py:32`）
**"Select providers and fail over only before streamed output becomes visible."**

```python
candidates = [primary, default, *fallbacks]
for name in candidates:
    provider = providers[name]
    if health[name].open_until > now:  # 熔断中，跳过
        continue
    forwarding_bus = _ForwardingBus(bus)  # 统计 token 数
    try:
        result = await provider.chat(..., forwarding_bus, ...)
        health[name] = _Health()  # 成功则重置健康状态
        return result
    except Exception as exc:
        health[name].failures += 1
        if failures >= threshold: health[name].open_until = now + cooldown_s
        if forwarding_bus.token_count > 0:  # 已经输出过 token！
            raise  # 不再降级，直接失败
```

- **_ForwardingBus**：包装 EventBus，拦截 `LlmTokenEvent` 计数——**这是"是否已产生可见输出"的唯一信号**。
- **熔断**：连续失败 2 次后进入 30s cooldown；成功调用后重置。
- **规则路由**：`strategy == "rule_based"` 时，若 messages >= 12 或 tool_schemas >= 10，切到 `complex_provider`（ stronger model）。

### TracingProvider（`trace/provider.py`）
- 装饰器模式包裹真实 Provider。
- 每次 `chat()` 前后写入 `TraceRecord`：请求方向（CORE→LLM）、响应方向（LLM→CORE）、延迟、完整 payload（可选脱敏）。
- 与事件总线互补：bus 发的是实时事件（TUI 消费），trace 是结构化审计日志（评测/调试消费）。

## 5. 为什么这样取舍

### 为什么流式输出后不能无缝切换模型？
- 主模型已经向用户输出了 `"我已经检查了项目，发现……"`。
- 网络断开 → 备用模型从头生成 → 用户看到两段回答拼接在一起，语义完全不连贯。
- Router 的 `_ForwardingBus.token_count` 就是检测"是否已经发过 token"的哨兵。一旦 > 0，任何异常都直接上抛，**宁可失败也不拼接**。
- 这是"可用性 vs 一致性"的取舍——我们选择一致性。

### 为什么 Anthropic 和 OpenAI 的消息格式要自己做转换？
- Anthropic：messages 是 `{"role": "assistant", "content": [{"type": "tool_use", "id": ..., "name": ..., "input": {...}}]}`
- OpenAI：messages 是 `{"role": "assistant", "tool_calls": [{"id": ..., "type": "function", "function": {"name": ..., "arguments": "json_string"}}]}`
- 两者的 `tool_result` 也完全不同（Anthropic 是 user message 的 content block，OpenAI 是独立的 `"role": "tool"` 消息）。
- 统一成 `LlmResponse` + `ToolCallBlock` 后，上层（AgentLoop、ContextEngine）完全不感知 Provider 差异。

### 为什么流式 tool_calls 要增量拼接？
- OpenAI 的 SSE stream 中，tool_calls 分多片到达：`{"index": 0, "id": "call-1", "function": {"name": "read_file", "arguments": "{\"pa"}}`，下一片补 `th\": \"README.md\"}`。
- 用 `partial_calls: dict[int, dict]` 按 index 累加，最后 `json.loads` 解析 arguments。
- Anthropic 不这样——它的 stream API 直接给完整 message，不需要增量拼接。

### 为什么熔断状态保存在内存里？
- ProviderRouter 是单例，health 字典在进程内存中。重启 Core 后熔断状态重置——这是合理的，因为重启通常意味着网络环境变了。
- 不持久化到磁盘：熔断是瞬时的防御机制，不需要跨进程共享。

### 为什么规则路由只依据 messages 数量和 tool 数量？
- 当前实现是启发式：`messages >= 12` 说明对话已经很长，需要更强模型的上下文理解能力；`tool_schemas >= 10` 说明工具空间复杂，需要更强的推理能力。
- 没有动态负载测试：规则路由是静态的，不根据实时成功率调整。

### 为什么不是动态修改配置后立即切换所有请求？
- 配置修改只影响**新创建的** ProviderRouter 实例——现有 Run 的 router 引用不会变。
- 这是故意的：正在跑的 Run 不应该因为配置变更而突然切换模型——可能导致上下文格式不兼容或行为突变。
- 新 Run 才会读到新配置。

## 6. 当前限制
- **熔断只按失败次数，不按失败类型**：网络错误和 API 限流都计同一次 failure。
- **规则路由太简单**：只有 messages 数和 tool 数两个维度，没有考虑任务复杂度、历史成功率。
- **OpenAI provider 没有重试机制**：AnthropicProvider 有 3 次流式重试，OpenAICompatibleProvider 没有。
- **TracingProvider 没有采样控制**：每次调用都记，高并发时 trace 文件会膨胀。
- **没有 Provider 性能基准**：不知道 Anthropic vs OpenAI 在相同任务上的延迟/成功率差异。

## 7. 如何测试

| 测试文件 | 验证什么 |
|---------|----------|
| `test_llm_provider.py` | Anthropic 流式调用、token 事件发布、thinking blocks 保留、cache marker 注入 |
| `test_openai_provider.py::test_openai_compatible_stream_accumulates_text_tool_calls_and_usage` | OpenAI SSE 流式解析、tool_calls 增量拼接、text 累加、usage 提取 |
| `test_provider_router.py::test_router_falls_back_before_any_token_is_visible` | 无 token 输出时正确降级到 fallback |
| `test_provider_router.py::test_router_never_falls_back_after_partial_stream_output` | 已输出 token 后异常 → **不再降级**，直接抛异常 |
| `test_provider_router.py::test_rule_router_selects_complex_provider` | messages >= 12 时切到 complex_provider |
| `test_tracing_provider.py` | 请求/响应双向记录、延迟计算、payload 包含/脱敏 |

## 8. 后续如何升级
- **按错误类型分级熔断**：网络错误（短 cooldown）、限流（长 cooldown）、认证错误（永久熔断）。
- **动态规则路由**：从 trajectory metrics 中按模型 × 任务类型维护成功率矩阵，自动调整路由规则。
- **OpenAI 流式重试**：补齐与 Anthropic 相同的网络层重试机制。
- **Trace 采样**：按 run_id hash 做 10% 采样，降低 I/O 开销。
- **Provider A/B 测试**：同一条消息同时发给两个 Provider，对比延迟和输出质量。

---

## 7 个核心问题速答

### Q1：Anthropic 与 OpenAI 工具调用格式有什么区别？
| | Anthropic | OpenAI |
|--|-----------|--------|
| **工具声明** | `tools: [{"name": ..., "description": ..., "input_schema": {...}}]` | `tools: [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}]` |
| **assistant 调用** | `content: [{"type": "tool_use", "id": ..., "name": ..., "input": {...}}]` | `tool_calls: [{"id": ..., "type": "function", "function": {"name": ..., "arguments": "json_string"}}]` |
| **tool result** | user message 的 content block：`{"type": "tool_result", "tool_use_id": ..., "content": ...}` | 独立消息：`{"role": "tool", "tool_call_id": ..., "content": ...}` |
| **流式** | `messages.stream()` 直接给完整 message | SSE `data:` 行，tool_calls 分片增量到达 |
| **缓存** | `cache_control: {"type": "ephemeral"}` | 不支持 |

### Q2：Provider 如何转换成统一 `LlmResponse`？
- `AnthropicProvider`：从 `final_message` 提取 `stop_reason`、`content` blocks（`tool_use`/`thinking`/`text`）、`usage`。
- `OpenAICompatibleProvider`：从 SSE chunks 累加 `text` 和 `tool_calls`（按 index 聚合），最后根据 `finish_reason` 映射到 `stop_reason`。
- 两者都返回相同的 `LlmResponse` dataclass，上层完全不感知差异。

### Q3：流式工具参数如何增量拼接？
- OpenAI 的 SSE 中，`delta.tool_calls` 按 `index` 分片到达（如先给 `{"index": 0, "function": {"arguments": "{\"pa"}}`，再给 `{"arguments": "th\": \"README.md\"}"`）。
- `partial_calls: dict[int, dict]` 按 index 累加 `id`、`name`、`arguments`。
- 流结束后 `json.loads(partial["arguments"])` 解析为 dict。
- Anthropic 不需要——它的 stream API 直接返回完整 message。

### Q4：为什么流式输出后不能无缝切换模型？
- 主模型已经向用户输出了部分文本（通过 `LlmTokenEvent`）。
- `_ForwardingBus` 统计 token_count > 0 时，任何异常都直接上抛，不再尝试 fallback。
- 如果切换，备用模型从头生成，用户看到两段不连贯的回答拼接——这是不可接受的 UX。
- 宁可让当前 Run 失败，也不破坏输出一致性。

### Q5：熔断状态保存在哪里？
- `ProviderRouter._health: dict[str, _Health]`，内存字典。
- `_Health` 含 `failures`（连续失败次数）和 `open_until`（熔断到期时间戳）。
- 连续失败 2 次后进入 30s cooldown；成功调用后重置。
- 进程重启后状态重置——合理的，因为重启通常意味着环境变了。

### Q6：当前规则路由依据是什么？
- `strategy == "rule_based"` 时：
  - `len(messages) >= 12` → 对话长，需要强模型的上下文能力
  - `len(tool_schemas) >= 10` → 工具空间复杂，需要强模型的推理能力
  - 满足任一条件就切到 `complex_provider`
- 否则用 `default_provider`。
- 这是静态启发式，没有实时成功率反馈。

### Q7：为什么不是动态修改配置后立即切换所有请求？
- 配置修改只影响**新创建的** ProviderRouter。
- 正在跑的 Run 持有的 router 引用不变——避免运行中突然切换模型导致行为突变或格式不兼容。
- 新 Run 才会读到新配置。这是"运行中稳定性"与"配置即时生效"之间的取舍。