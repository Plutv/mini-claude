# Context Engine 面试卡 — 简历第三点

> 对应代码：`src/kama_claude/core/compact/engine.py`、`core/compact/budget.py`、`core/compact/compactor.py`、`core/tools/artifacts.py`、`core/session/store.py`。  
> 真实实验参考：`tests/unit/test_context_engine.py`、`test_tool_artifacts.py`、`test_budget.py`、`test_compactor.py`。

---

## 1. 解决的问题
Agent 长任务中，每次 step 都把工具结果和 LLM 回复追加到 messages，上下文长度持续增长。目标是**在不破坏 Anthropic tool-use 协议、不影响任务成功率的前提下，持续降低上下文载荷**。

## 2. 最初方案
超过窗口上限时一次性调用 LLM 做全文摘要。

## 3. 暴露的问题
- **一次性摘要太晚**：85% 水位触发的全量摘要本身也要消耗大量 token，窗口已快满时再做摘要不稳定。
- **大工具结果直接撑爆窗口**：单次 read_file 可能 512KB（~13万字符），远大于窗口。
- **错误结果不能丢**：`bash` 报错里可能有关键错误信息，摘要会把它删掉。
- **压缩过程出错 = 会话损坏**：如果摘要 LLM 挂了，messages 可能已经改了一半。

## 4. 最终设计：四层 Context Engine

```text
水位    层级              行为
───────────────────────────────────────────────────────────
50%     Budget (第1层)    超大 inline tool result 做 head+tail 截断
60%     Snip (第2层)      清理陈旧结果（旧文件读取、旧目录列取）
70%     Microcompact (第3层)  按周期 + idle 条件做 aggressive 陈旧清理
85%     Full Compact (第4层)  调用 LLM 做全量摘要，替换为 summary + continuation
```

核心入口 `ContextEngine.prepare()`（`engine.py:98`），关键策略：

### 第 1 层：Budget — 超大 inline 截断
- `budget_tool_results()`（`budget.py:84`）：遍历所有 `tool_result`，若 `len(content) > limit(=8000)`，则截断为 `head + [omitted] + tail`，保留 `keep(=4000)` 字符。
- **效果**：模型仍能读开头和结尾，丢失的是中间部分——通常代码开头是 import/声明，结尾是总结/测试输出。

### 第 2 层：Snip — 陈旧结果清理
- `snip_stale_tool_results()`（`budget.py:114`），非 aggressive 模式：
  - 只清理 `bash`、`grep_search`、`list_dir`、`list_files`、`read_file`、`run_shell`、`search` 这 7 种工具的结果。
  - **保留**：错误结果（`is_error=True`）、非 snippable 工具结果。
  - **保留最近 N 个**（默认 3 个）。
  - **额外规则**：同一文件的重复读取，除最新外全部视为陈旧（`reads_by_path` 去重）。
  - **不删除 block**：把 `content` 替换为标记文本（`"[Previous read_file result removed...]"`），`tool_use_id` 和 `tool_result` block 本身保留——**不破坏协议配对**。

### 第 3 层：Microcompact — 按周期 aggressive 清理
- 触发条件：`utilization >= 70%` 且（idle（距离上次 API 调用 > 300s）或 `step % microcompact_every_steps == 0`）。
- 与第 2 层相同函数，但 `aggressive=True`：标记文本变成 `"[Old result cleared]"`，更激进。
- **设计意图**：不要让清理只在"快满了"才触发——早期定期清理能减缓增长曲线。

### 第 4 层：Full Compact — LLM 全量摘要
- `Compactor.compact()`（`compactor.py:73`）：
  - 把整个 messages 序列化为纯文本，喂给 LLM + 固定 prompt（6 段式摘要：Goal / Completed Steps / Key Constraints / Current File State / Remaining TODOs / Critical Data）。
  - 成功时替换 messages 为 3 条：`[Previous conversation summary]\n<summary>` + `Understood, I'll continue...` + `Continue the unfinished task...`。
  - 摘要写入 `summary_<ts>.md` 备份。
  - 发布 `context.compacted` 事件，metrics 计数。

### 全局策略
- **缓存热保护**：如果距离上次 API 调用 < 300s，60% snip 不触发（`cache_hot`），但 95% 以上时强制覆盖（`hot_cache_override`）。
- **深拷贝保护**：所有层都在 `deepcopy` 上操作，全量摘要失败时**恢复原始 messages**（`engine.py:186-187`）。
- **配对校验**：`tool_pairs_balanced()` 在压缩前检查每个 `tool_use` 都有唯一的 `tool_result`——不平衡时跳过所有清理（`engine.py:110-115`）。

### Artifact 外置（与 Context Engine 联动）
- `ToolArtifactStore.externalize()`（`artifacts.py:32`）：
  - 若工具结果字节数 > `threshold`（默认 32KB，水位越高 threshold 越低），写入文件系统：`artifacts/<tool_name>-<tool_use_id>.txt` + `.json` 元数据（含 sha256）。
  - 模型上下文里只留：前/后各 2K 字符预览 + 路径 + sha256 + 提示"用 read_file 读完整内容"。
  - 错误结果**不外置**（`if result.is_error: return result`）。
  - threshold 动态：context_pct >= 70% → 8K；>= 50% → 16K；< 50% → 32K（`engine.py:84-90`）。

## 5. 为什么这样取舍

### 为什么不是一次 85% 才摘要？
- 窗口到 85% 时，LLM 摘要请求本身就可能超限。
- 大工具结果（如 `read_file` 返回 512KB）即使只占一次调用，也可能直接撑爆窗口。
- 分层提前减压：50% 开始截断、60% 开始清理、70% 周期清理——让 85% 全量摘要有**足够的 token 余量**完成。

### 为什么只清理特定 7 种工具？
- 这些工具的结果通常是**可重新计算的**（再读一次文件、再列一次目录）。
- `write_file`、`bash` 等副作用工具的结果不清理——它们包含错误信息或写入确认，丢了不可恢复。

### 为什么错误结果不清理？
- LLM 看到错误才能调整策略。如果 snip 把 `bash: command not found` 换成 `[Previous bash result removed]`，LLM 就不知道之前做错了什么。

### 为什么 microcompact 需要 idle 或周期条件？
- 连续多轮对话时，频繁 aggressive 清理会丢失 LLM 刚读到的信息。
- 只在"用户没发消息"（idle）或"每 N 步"（周期）时才触发，**保证最近几轮的信息密度**。

### 为什么用 LLM 做摘要而不是固定模板？
- 固定模板无法提取"哪些文件改了、哪些错误是关键"。LLM 摘要能保留 handoff 所需的关键决策和状态。
- 代价是：摘要失败时回退到原始 messages（`deepcopy` 保护）。

## 6. 当前限制
- **不能自动判定"哪些信息对任务最重要"**：只能按工具类型和 recency 做启发式清理。
- **全量摘要本身消耗 token**：摘要 prompt + 完整历史文本也要发给 LLM，窗口接近 100% 时可能自身超限。
- **摘要质量不可控**：LLM 可能遗漏 Critical Data（如 exact error message），摘要后任务成功率可能下降——这是重点需要验证的。
- **Artifact 文件无 GC**：长期运行后 artifacts 目录会膨胀，没有自动清理旧 artifact。
- **Checkpoint 只保证消息原子性**：`write_messages_atomic()` 用 tmp + fsync + replace 保证 JSONL 不半写，但**不保证外部文件系统状态**（如 bash 改过的文件）。

## 7. 如何测试

| 测试文件 | 验证什么 |
|---------|----------|
| `test_context_engine.py::test_stale_cleanup_preserves_tool_protocol_pairs` | snip 后 tool_use ↔ tool_result 配对仍然完整 |
| `test_context_engine.py::test_hot_cache_defers_stale_prefix_rewrite` | cache_hot 时 60% snip 不触发，保护最近信息 |
| `test_context_engine.py::test_multi_round_microcompact_keeps_recent_results` | 周期 microcompact 只清旧结果，保留最近 2 个 |
| `test_context_engine.py::test_full_compaction_failure_restores_exact_history` | **全量摘要失败时 messages 字节级恢复** |
| `test_context_engine.py::test_full_compaction_adds_safe_continuation_message` | 成功时 messages 替换为 summary + continuation |
| `test_context_engine.py::test_tool_artifact_threshold_tightens_as_context_fills` | context_pct 越高，artifact threshold 越低（更激进外置） |
| `test_tool_artifacts.py::test_large_tool_output_is_persisted_and_replaced_by_reference` | 大结果外置为文件，上下文只留引用 |
| `test_tool_artifacts.py::test_small_tool_output_stays_inline` | 小结果不外置 |
| `test_tool_artifacts.py::test_hundred_thousand_character_output_reduces_by_over_ninety_percent` | 10 万字符压力测试验证载荷缩减 95% 以上；这是资源指标，不证明回答质量 |
| `evals.context_quality_eval` | 6 类问答重复 3 轮：Exact Match `100% → 100%`，累计 Prompt Token `562,599 → 146,022`（`-74.05%`） |
| `test_budget.py` | budget/snip 的 token 估算和字符截断 |
| `test_compactor.py` | 摘要 prompt 格式、文本序列化、summary 写入 |

## 8. 后续如何升级
- **摘要质量回归**：对同一批历史做"原始 messages → 摘要 → 继续任务" vs "原始 messages → 继续任务" 对比，验证摘要是否导致成功率显著下降。
- **关键信息保留策略**：在 snip 前用轻量模型提取"不可丢失的信息"（如文件路径、错误码），附在标记文本上。
- **Artifact GC**：按 session 关闭时清理未被后续 step 引用的 artifact。
- **分层摘要**：microcompact 也用轻量模型做局部摘要（如"前 3 步做了什么"），而不是直接替换为标记文本。
- **自适应 threshold**：根据工具成功率反馈动态调整各层 threshold（如某工具经常失败后 LLM 重试，则提高该工具的保留优先级）。

---

## 7 个核心问题速答

### Q1：为什么不能只在 85% 时总结一次？
- 窗口到 85% 时，LLM 摘要请求本身可能超限（要发完整历史）。
- 单层截断无法处理"单次工具结果就 512KB"的情况。
- 四层提前减压（50/60/70/85），让 85% 全量摘要有余量完成，同时早期清理减缓增长曲线。

### Q2：Artifact 保存了什么，模型上下文留下什么？
- **保存**：完整工具结果写入 `artifacts/<tool>-<id>.txt` + `.json` 元数据（bytes、sha256）。
- **上下文留下**：前/后各 2K 字符预览 + `"[large tool result stored as artifact]"` + 路径 + sha256 + 提示用 `read_artifact` 按 query 或范围补取。
- 超限的成功结果和错误结果都会外置，并保留 `is_error/error_type` 语义。

### Q3：什么工具结果会被判定为陈旧？
- **只清理 7 种可重算工具**：`bash`、`grep_search`、`list_dir`、`list_files`、`read_file`、`run_shell`、`search`。
- **不清理**：错误结果（`is_error=True`）、副作用工具（`write_file`、`note_save` 等）。
- **额外规则**：同一文件重复读取时，旧读取全部陈旧（`reads_by_path` 去重）。
- **保留最近 N 个**（默认 3 个），aggressive 模式保留最近 3 个但标记文本更短。

### Q4：Microcompact 和全量摘要有什么区别？
| | Microcompact (第 3 层) | Full Compact (第 4 层) |
|--|------------------------|------------------------|
| **触发** | utilization >= 70% + (idle > 300s 或 step % 4 == 0) | utilization >= 85% |
| **机制** | `snip_stale_tool_results(aggressive=True)`：把旧结果 content 换成 `"[Old result cleared]"` | 调用 LLM 生成 6 段式摘要 |
| **协议** | 保留所有 tool_use/tool_result block | 替换为 3 条新消息 |
| **信息损失** | 旧结果内容丢失，但 block 存在 | 全部历史语义压缩为摘要 |
| **失败回退** | 不涉及（只是改 content） | 失败时恢复 deepcopy |

### Q5：为什么不能拆断 tool_use/tool_result？
- Anthropic API 协议硬约束：assistant 消息里出现 `tool_use` 后，下一条 user 必须含对应 `tool_use_id` 的 `tool_result`。
- `snip` **不删除 block**，只把 `content` 换成标记文本——`tool_use_id` 和 `tool_result` block 保留。
- `tool_pairs_balanced()` 在压缩前检查：每个 `tool_use` 有唯一 `tool_result`；不平衡时**跳过所有清理**。
- `SessionStore._trim_orphan_tool_use()` 在读取时裁掉尾部未配对的 tool_use。

### Q6：压缩失败如何避免破坏原历史？
- `prepare()` 开头 `original = deepcopy(context.messages)`（`engine.py:109`）。
- 所有前 3 层都在 `deepcopy` 的 `working` 上操作。
- 第 4 层 `compactor.compact()` 若失败（抛异常或返回 None）：`context.messages = original`（`engine.py:187`）。
- 成功后才 `context.messages = working`（`engine.py:205`）。
- 测试验证：`test_full_compaction_failure_restores_exact_history` 断言失败后 messages 与原 deepcopy 完全一致。

### Q7：当前 Checkpoint 能保证什么、不能保证什么？
- **能保证**：
  - `write_messages_atomic()` 用 tmp + `fsync` + `os.replace` 保证 JSONL 文件**不半写**。
  - `tool_pairs_balanced()` 检查后才写入，**不会写入不配对的历史**。
  - step 边界调用 checkpoint，**每个持久化的 step 都是协议安全的**。
- **不能保证**：
  - 外部副作用 exactly-once（如 bash 命令已经执行了，但 checkpoint 在之后——崩溃后重跑会再执行）。
  - 不是任意位置断点续跑——只在 step 边界快照。
  - 事件回放是"读历史事件"，不是"重新执行 Agent"——外部状态（文件系统）可能已经变了。
