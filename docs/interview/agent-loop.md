# Agent Loop 面试卡 — 简历第二点

> 对应代码：`src/kama_claude/core/loop.py`、`core/tools/invocation.py`、`core/tools/registry.py`。
> 配套：`core/context.py`（ExecutionContext）、`core/tools/base.py`（BaseTool/ToolResult）、`core/tools/builtin/*`。
> 真实实验参考：`tests/unit/test_loop.py`、`test_invocation.py`、`test_tool_parallelism.py`、`test_tool_retry.py`。

---

## 1. 解决的问题
让 LLM 能在一个长会话里反复**推理 → 调工具 → 看结果 → 再推理**，且：
- 多个互不依赖的工具必须能并发跑（不浪费时延）。
- 单个工具的失败不能把整个 Run 拉下水——LLM 应该看见错误再决策。
- 工具协议必须严格符合 Anthropic 协议（tool_use ↔ tool_result 强配对）。
- 客户端断开时，背景 Run 必须能干净取消。

## 2. 最初方案
同步循环：`chat() → parse tool name → run tool → chat() → ...`。每轮串行调用。

## 3. 暴露的问题
- 多个独立 read_file 串行浪费等待时延。
- 工具异常直接冒泡，循环崩，LLM 没有"看一眼错误重试"的能力。
- 协议层不清晰：超时/被拒/路径错误都是异常栈，前端没法渲染。
- 客户端断网时无法取消后台 Run。

## 4. 最终设计
**`AgentLoop.run(context)`** 是 `while not context.is_done()` 的循环，每步做四件事：

```text
maintain → plan → observe → act → checkpoint
   ↓         ↓        ↓        ↓        ↓
 context    LLM      推回    parallel  JSONL
 engine   .chat()   blocks   safe      边界
 .prepare          →context   batch    落盘
```

- **maintain**（可选）：`ContextEngine.prepare()` 做陈旧清理 + micro compact + 高水位全量摘要。
- **plan**：`provider.chat(messages, tool_schemas, ...)`，异常 → `mark_failed("llm_error")` 跳出；`CancelledError` → 标记后**重抛**。
- **observe**：把 `thinking_blocks` + `text` + `tool_use` 拼成 assistant 消息追加。
- **act**：`stop_reason == "tool_use"` 才执行；`_invoke_requested_tools` 按 `parallel_safe` 切 batch → `asyncio.gather` 并行 / 串行单个。
- **terminate**：`end_turn` → `mark_success`；`step >= max_steps` → `mark_failed("exceeded_max_steps")`；**end_turn 优先于 max_steps**。
- **checkpoint**：只在 step 边界持久化消息，**保证 tool_use ↔ tool_result 配对**。

**`_invoke_requested_tools`** 扫描算法：

```python
while index < len(tool_calls):
    tool = registry.get(tool_calls[index].name)
    if tool is None or not tool.parallel_safe:
        await _invoke_one(...)           # 串行单个
        index += 1
        continue
    # 收集连续 parallel_safe 到 batch
    end = index
    while end < len(tool_calls):
        candidate = registry.get(tool_calls[end].name)
        if candidate is None or not candidate.parallel_safe: break
        end += 1
    batch = tool_calls[index:end]
    if len(batch) == 1:
        await _invoke_one(batch[0])      # 避免 gather 开销
    else:
        await asyncio.gather(*(_invoke_one(c, ...) for c in batch))
    index = end
```

**`invoke_tool`** 的失败契约（永不抛异常给 Loop）：

| 触发 | `error_type` | 是否重试 | 来源 |
|------|--------------|----------|------|
| unknown tool | `runtime_error` | 否 | registry.get() 为 None |
| `plan_controller.guard()` 拦截 | `plan_mode_denied` | 否 | plan 模式未批准 |
| Pydantic ValidationError | `schema_error` | 否 | 参数对不上 |
| `permission_manager` 拒绝 | `permission_denied` | 否 | 用户拒绝 |
| action 用完 | `action_budget_exceeded` | 否 | plan 模式 action 计数 |
| `asyncio.TimeoutError` | `timeout` | 否 | `asyncio.wait_for(timeout)` |
| `RateLimitedError` | `rate_limited` | **是**（最多 2 次） | 退避 base=2s |
| 其它 `Exception` | `runtime_error` | **是**（最多 2 次） | 退避 base=2s |
| 工具主动返回 `is_error=True` | `error_type` | 否（已写好返回） | 业务失败 |

每个失败都发 `ToolCallFailedEvent`；每个成功都发 `ToolCallFinishedEvent`；**always 先发 `ToolCallStartedEvent`**。

## 5. 为什么这样取舍
- **显式 `parallel_safe` 声明**：静态分析"有没有副作用"在 Python 生态不可靠，作者写 `parallel_safe = True` 是最便宜的合约（默认 `False`，内置只有 `read_file` 开了）。
- **永不抛异常给 Loop**：让 LLM 看到 `is_error=True` 自行调整，符合 agent 的"自愈"语义；如果抛了，整个 Run 失去上下文。
- **可重试仅限 `runtime_error / rate_limited`**：`schema_error` 是参数错（重试无意义），`permission_denied` 是用户决定（再问一次烦人），`timeout` 是工具侧问题（重试只是又卡一次）。
- **`CancelledError` 必须上抛**：客户端断网 → TCP 关 → asyncio 取消该 Run 的 task；如果吞了就是泄漏。Python 3.8+ 它是 `BaseException` 的子类，但代码显式 `try/except` 是为了**先标记 status 再抛**。
- **`max_tokens` 截断时合成 error tool_result**（loop.py:182-192）：保留 Anthropic 协议要求的 tool_use ↔ tool_result 配对，避免下一次 chat 报错。
- **checkpoint 只在 step 边界**：daemon 在 step 中途崩溃，下一次重启时上一次完整 step 是原子的；不会留下孤儿 `tool_use`。
- **end_turn 优先于 max_steps**：保证"任务已答完 + step 数恰好到顶"也算成功，而不是莫名其妙失败。

## 6. 当前限制
- **不能自动判定副作用**：所有新工具作者必须主动 `parallel_safe = True`。
- **batch 切分只看相邻 parallel_safe**：无法表达"前面 read_file + 后面 write_file 中间要按顺序"的依赖——目前交由 LLM 自行决定调用顺序。
- **`max_tokens` 截断的合成提示是固定的字符串**："请拆成更小的步骤"，无法洞察模型本来要干什么。
- **重试只看 `error_type`，不看内容**：所有 `rate_limited` 都按 base=2s 退避，没有"区分 429/503"。
- **没有 sub-step 中断**：并行 batch 中的一个工具被取消，其他兄弟也会被一起取消。

## 7. 如何测试
| 测试文件 | 验证什么 |
|---------|----------|
| `tests/unit/test_loop.py::test_end_turn_marks_success` | end_turn 单步终止 |
| `tests/unit/test_loop.py::test_tool_use_then_end_turn_marks_success` | tool_use → end_turn 两步路径 |
| `tests/unit/test_loop.py::test_tool_failure_loop_continues_to_success` | 工具 fail 后 LLM 仍可 end_turn |
| `tests/unit/test_loop.py::test_tool_result_appended_to_context` | tool_result 格式正确 |
| `tests/unit/test_loop.py::test_max_steps_marks_failed` | max_steps 终止 + 原因正确 |
| `tests/unit/test_loop.py::test_cancelled_error_marks_failed_and_reraises` | CancelledError 标记后重抛 |
| `tests/unit/test_loop.py::test_llm_api_error_marks_failed` | LLM 异常吞掉 + 标记 llm_error |
| `tests/unit/test_loop.py::test_step_started_and_finished_events_published` | step 级事件可观测 |
| `tests/unit/test_tool_parallelism.py::test_parallel_safe_tools_overlap_but_results_keep_request_order` | 用 `asyncio.Event` 真证明两个工具**同时启动** |
| `tests/unit/test_tool_parallelism.py::test_tools_without_parallel_safe_run_sequentially` | 非 parallel_safe 严格串行 |
| `tests/unit/test_invocation.py::test_unknown_tool_returns_runtime_error` | unknown tool 失败路径 |
| `tests/unit/test_invocation.py::test_missing_required_param_gives_schema_error` | schema 错误分类 |
| `tests/unit/test_invocation.py::test_timeout_gives_timeout_error` | timeout 路径 |
| `tests/unit/test_invocation.py::test_runtime_exception_gives_runtime_error` | 工具 raise 不外抛 |
| `tests/unit/test_invocation.py::test_started_event_always_first` | started 永远先发 |
| `tests/unit/test_tool_retry.py` | 指数退避、重试次数上限 |

## 8. 后续如何升级
- **重试策略细化**：按工具名 × error_type 维护成功率矩阵，热点工具独立退避曲线（如网络类 1s→4s→10s，限流类 5s→30s）。
- **parallel_safe 静态推断**：从工具签名（只有参数是 path） + 沙箱 I/O 测试推断，替代手动声明。
- **partial failure 模型**：长工具可"中断 + 续跑"，而不是取消整个 batch。
- **cancel 精度提升**：细分"取消当前 step"和"取消整个 run"，让并行 batch 里的非目标工具可继续。
- **Checkpoint 增量**：除了消息 JSONL，对工具中间状态（如 bash 的子进程 PID）做快照，恢复时能续跑而不是从头。

---

## 6 个核心问题速答（面试对话用）

### Q1：ReAct 在代码里如何体现？
- **Reason** = `provider.chat(messages, ...)`，把当前 `context.messages` 整段发出去。
- **Act** = 若 `stop_reason == "tool_use"`，调 `_invoke_requested_tools`。
- **Observe** = 把工具结果 `add_tool_result(...)` 追加为 user 消息。
- 终止 = `end_turn` → `mark_success`。
- 这就是教科书 ReAct（Reason-Act-Observe）。我没有用 LangGraph/ReAct 库，就是 while 循环自己拼。

### Q2：模型如何表达工具调用？
- 响应里两个字段：`stop_reason`（决定接下来做什么）+ `tool_calls: list[ToolCallBlock]`。
- 每个 `ToolCallBlock` 含 `id`（必须回填到 tool_result）、`name`（registry 查找）、`input`（已解析 dict）。
- 模型在工具 schema 那一侧通过 `registry.tool_schemas()` 看到 Anthropic 格式 `input_schema`。

### Q3：为什么工具结果必须作为 `tool_result` 回填？
- Anthropic API 协议硬约束：assistant 里出现 `tool_use` 后，下一条 user 必须含对应 `tool_use_id` 的 `tool_result`，否则下次 chat 报 400。
- Loop 用 `add_tool_result` 把同一步多个结果合并到**同一个 user 消息**（content 是 list of blocks），省 token。
- 边界处理：`max_tokens` 截断时主动写 synthetic error tool_result（loop.py:182-192），**保证配对**。

### Q4：多个工具如何决定并行或串行？
- **不靠系统分析副作用**——工具**显式声明** `parallel_safe = True`（默认 `False`）。
- `_invoke_requested_tools` 从左到右扫描，连续 `parallel_safe` 组成 batch，`asyncio.gather` 并行；遇到非 safe 就串行单个 + 作为新 batch 起点。
- batch=1 时不走 gather（避免调度开销）。
- 内置只有 `read_file` 声明了 `True`（bash / write_file / list_dir / task_* 都默认 False）。

### Q5：工具失败为什么不一定立即终止 Run？
- `invoke_tool` 的契约是**永不抛异常给 Loop**。所有失败都包装成 `ToolResult(is_error=True)` 回填到 context（`add_tool_result(..., is_error=True)`）。
- LLM 看到错误后可以：换工具、改参数、问用户。
- 例外能重试的：`runtime_error`、`rate_limited`（指数退避，最多 2 次）。
- **真正终止 Loop 的只有**：LLM API 异常 → `llm_error`；`CancelledError` → `cancelled`；`step >= max_steps` → `exceeded_max_steps`。
- "agent 区别于脚本" 的核心：失败可恢复。

### Q6：`CancelledError` 为什么必须向上传播？
- 客户端 TCP 断开时，asyncio 会取消对应的 Run task → 触发 `CancelledError`。
- 如果吞了，后台 Run 会一直跑，泄漏资源 + 永远不结束。
- Python 3.8+ `CancelledError` 是 `BaseException` 子类，本来就不会被 `except Exception` 捕获；但代码显式 `try/except` 是为了**先 `mark_failed("cancelled")` 再 raise**，让 status 可被查询。

---

## 真实实验 5 个（对照 test_*.py）
1. **end_turn**：单步 provider 直接 `end_turn` → `status=success`, `step=1`（`test_end_turn_marks_success`）。
2. **tool_use → tool_result → end_turn**：provider 序列 `[tool_use(echo), end_turn]` → `messages[2]` 是 `tool_result` user 消息，`tool_use_id="t1"`, `content="hi"`（`test_tool_use_then_end_turn_marks_success` + `test_tool_result_appended_to_context`）。
3. **工具超时**：`_SlowTool` 永久 sleep + `timeout=0.05` → `error_type="timeout"`，发布 `tool.call_failed`（`test_timeout_gives_timeout_error`）。
4. **并行 read_file**：两个 `_ParallelProbeTool` + 共享 `asyncio.Event`，第二个 invoke 时 set event → 两个工具**同时 started**，结果按**模型请求顺序**返回（`test_parallel_safe_tools_overlap_but_results_keep_request_order`）。
5. **权限拒绝**：`PermissionManager` 拒绝 → `error_type="permission_denied"`，错误信息明确告诉 LLM"你不能执行这个命令，试试别的方法或问用户"（实际测试在 `test_permission_manager.py` / `test_permission_policy.py`）。

---

## 附录：轨迹评测与故障注入（简历第五条）

### 解决的问题
Agent 的执行质量不能只看"是否 end_turn"，需要定量验证：工具成功/失败率、重试次数、Token 消耗、延迟分布、上下文压缩频率，以及异常注入后的恢复行为。

### 最终设计
**`TrajectoryMetrics`**（`core/eval/trajectory.py:49`）从 `runs/<run_id>/events.jsonl` 逐行解析事件流，聚合 18 个指标：

| 类别 | 指标 | 来源事件 |
|------|------|----------|
| 生命周期 | run_id, status, reason, steps, duration_ms | `run.started` / `run.finished` |
| 工具 | tool_calls, tool_successes, tool_failures, retry_attempts, incomplete_tool_calls | `tool.call_started` / `finished` / `failed` |
| 权限 | permission_requests, permission_denials | `permission.requested` / `denied` |
| 子 Agent | subagents_started, subagents_finished | `subagent.started` / `finished` |
| 上下文 | context_compactions | `context.compacted` |
| Token | input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens | `llm.usage` |
| 模型 | models（列表，含路由切换记录） | `llm.model_selected` |

**故障注入**（`core/eval/faults.py`）：装饰器模式，包装 LLMProvider 和 BaseTool。

```python
@dataclass(frozen=True)
class FaultSpec:
    at_call: int          # 第几次调用时触发
    kind: FaultKind       # timeout | connection | rate_limit | runtime
    latency_ms: int = 0   # 注入前先 sleep（模拟延迟）
```

- `FaultInjectingProvider`：第 N 次 `provider.chat()` 时抛指定异常。
- `FaultInjectingTool`：第 N 次 `tool.invoke()` 时抛指定异常。

**支持的故障类型**：
- `timeout` → 抛 `TimeoutError`
- `connection` → 抛 `ConnectionError`
- `rate_limit` → 抛 `RateLimitedError`（会被 invoke_tool 的退避重试机制捕获）
- `runtime` → 抛 `RuntimeError`

### 为什么这样取舍
- **事件驱动评测**：不修改 Loop 代码，从现有事件流推断状态——与 AgentLoop 的 "事件总线" 设计自然衔接。
- **按调用计数注入**：精确控制故障点（"第 2 次调用限流"），而不是随机注入——可复现、可回归。
- **pending_tools 追踪**：通过 `started` 加入 set、`finished` 移除，最终统计 `incomplete_tool_calls` ——发现 daemon crash 导致的孤儿工具调用。
- **retry_attempts 去重**：`failed_attempts` 字典计数，只在最终 `finished` 时加总——避免把每次重试都算作独立失败。

### 当前限制
- 故障注入目前只验证**确定性异常**（第 N 次调用抛异常），不是随机混沌工程。
- `malformed_rows` 计数但不修复——事件文件损坏时只能跳过。
- 只读事件流，**不重新执行**——评测的是"已发生的轨迹"，不是"重跑一致性"。
- 没有跨 Run 聚合——每个 Run 的 metrics 是独立的，没有汇总 Dashboard。

### 测试覆盖
- `tests/unit/test_trajectory_eval.py`：从 JSONL 文件解析并验证 metrics 准确性
- `tests/unit/test_fault_injection.py`：验证 `FaultInjectingProvider` / `FaultInjectingTool` 在指定 call 数时正确抛异常

### 后续升级
- 跨 Run 聚合：按工具名 × error_type 维护成功率矩阵，驱动重试策略优化。
- 混沌注入：从确定性改为按概率分布注入（10% 超时 + 5% 限流），模拟真实环境。
- 回归基线：将每次 CI 的 trajectory metrics 与历史基线 diff，自动标记退化。