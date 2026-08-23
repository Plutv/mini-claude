# KamaClaude Evaluation 体系总结

> 用途：面试前把"评测"这件事说清楚——它验证了什么、没验证什么、为什么这么设计。
> 定位原则（与简历一致）：**写清问题、机制和关键取舍；面试主动说明能力边界。**

项目里有**两套独立但互补**的评测，别混为一谈：

| 层 | 位置 | 本质 | 跑不跑真实模型 |
|---|---|---|---|
| 1. 轨迹评测 + 故障注入 | `src/kama_claude/core/eval/` | 离线、单 run 的事件重放分析 | 否（分析已发生的 run） |
| 2. 系统能力 + 任务数据集 | `evals/` | 确定性机制评测 + 编码任务 harness | 部分否（system_eval 不调模型）；harness 跑模型但尚无真实结果 |

---

## Layer 1：`core/eval/` — 轨迹评测与故障注入

### `trajectory.py` — 轨迹指标聚合
- `evaluate_trajectory(path)` 读取一个 `events.jsonl`，**逐行重放事件、聚合 18 个指标**到 `TrajectoryMetrics`。
- 关键：**是"事件重放"，不是"重新执行"**。Agent 实际跑完留下 `events.jsonl`，这个函数是事后离线分析，不重新调用模型或工具。
- 覆盖的事件类型：`run.started/finished`、`step.finished`、`tool.call_started/finished/failed`、`permission.requested/denied`、`subagent.started/finished`、`context.compacted`、`llm.model_selected`、`llm.usage`。
- 18 个指标字段：`run_id, status, reason, steps, duration_ms, tool_calls, tool_successes, tool_failures, retry_attempts, permission_requests, permission_denials, subagents_started, subagents_finished, context_compactions, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, models[], tools[], incomplete_tool_calls[], malformed_rows`。
- 不变量处理：用 `pending_tools` 集合追踪未配对的 `tool_use_id`，跑完仍在集合里的即 `incomplete_tool_calls`（断点/崩溃残留）；`retry_attempts` 只统计同 tool_id 的失败重试，不计首次失败本身。

### `faults.py` — 故障注入
- `FaultSpec(at_call, kind, latency_ms)`；`FaultKind = timeout | connection | rate_limit | runtime`。
- `FaultInjectingProvider` / `FaultInjectingTool` 是两个**包装器（wrapper）**：包裹真实 provider/tool，在第 `at_call` 次调用时按 `kind` 抛错（可选先 `asyncio.sleep(latency_ms)` 模拟延迟）。
- 用途：把故障"钉"在第 N 次调用，验证重试策略、降级、原子恢复是否真的生效——是鲁棒性实验的注入手段。

### `suite.py` — 声明式用例
- `EvalCase`（name, events_path, expected_status, required_tools[], max_steps, max_input_tokens, max_tool_failures）+ `EvalSuite.evaluate()`。
- 跑完 `evaluate_trajectory` 后做断言式校验：`status`、`required_tools ⊆ tools`、`tool_pairs_complete`（无 incomplete）、`tool_failures ≤ max`、`step_budget`、`input_token_budget`。
- 这是把"一次 run 必须满足哪些不变量"写成可重复执行的检查。

---

## Layer 2：`evals/` — 系统能力评测 + 编码任务数据集

### `system_eval.py` — 确定性系统能力评测（**不调真实模型**）
- 直接构造合成 fixture，调用 Runtime 内部组件（ContextEngine、MemoryStore、SessionStore、ToolArtifactStore），断言行为正确。
- 3 个 suite、11 个 case，**最新运行 `evals/results/system_eval_latest.json`（2026-08-21, commit `bd89474`, dirty）：11/11 通过，pass_rate 1.0。**
- `context`（3 case）：
  - `large-success` / `large-error`：10 万字符结果外置为 artifact，校验 sha256 一致、错误语义保留、上下文载荷缩减 ~95.7%。
  - `layered-maintenance`：8 次工具历史下跑 ContextEngine，断言 `after < before`（缩减 85.56%）、当前请求被保留、`tool_pairs_balanced`、确实发生了 budget/microcompact。
- `memory`（3 case）：
  - `selective-retrieval`：5 条记忆召回，`Hit@1=1.0`、`Hit@3=1.0`、scope 零越界（跨 scope 不串）。
  - `content-deduplication`：相同内容二次写入返回同一 memory id、合并 tag。
  - `recall-character-budget`：召回受 `max_chars=100` 约束，只选 1 条、用 87 字符。
- `recovery`（5 case）：
  - `atomic-roundtrip`：写→读一致且配对平衡。
  - `reject-unbalanced-snapshot`：写入不配对历史抛 `ValueError`、旧快照保留。
  - `atomic-replace-failure`：注入 `os.replace` 失败，断言旧快照保留、无 `.tmp` 泄漏。
  - `broken-jsonl-tail`：尾部截断的坏行被跳过，有效消息完整恢复。
  - `orphan-tool-use-tail`：孤立 `tool_use`（无配对 `tool_result`）被裁剪，配对仍平衡。

### `harness.py` — 编码任务数据集（准备给真实 Agent 跑）
- 本地数据集 `datasets/local_v1/`，**6 个 case**：`config-precedence`、`async-pending-cleanup`、`idempotent-retry`、`jsonl-tail-recovery`、`rpc-response-routing`、`workspace-path-guard`（前 4 个 dev 划分、后 2 个 holdout 划分）。
- 每个 case 目录：`case.toml`（元数据）、`task.md`（任务描述）、`seed/`（初始**已坏**仓库）、`solution/`（gold 答案）、`oracle/`（隐藏测试）。
- 子命令：
  - `list` / `show`：列任务 / 看单个任务。
  - `prepare <id> --output`：只把 `seed/` + `TASK.md` 复制成干净工作区——**Agent 看不到 oracle/solution**。
  - `verify <id> --workspace`：跑 `pytest`（公开 `tests/` + 隐藏 `oracle/`）。
  - `validate`：证明每个 case "初始 seed 失败、gold solution 通过"（baseline_failed & gold_passed）。
- 这是真正的"代码任务解决率"评测入口，但**需要接真实模型跑**。

### 数据分层（`README.md` + `datasets/external_sources.toml`）
1. `local_v1`：仓库内自建、完全离线、可重复（开发集）。
2. QuixBugs：40 个 Python/Java 单行算法缺陷（轻量，recommended_next）。
3. Defects4J / GitBug-Java：真实 Java 项目缺陷（planned）。
4. SWE-bench Verified：真实 GitHub Issue，成本/环境最高，只做**最终外部效度验证**（final_validation_only）。

### 实验协议（`plan/experiment-protocol.md`）
- 研究问题：① KC 能否完成确定性测试判据的代码修改；② Context Engine v2 能否降开销且不降完成率；③ 权限/重试/子Agent 是否改善可靠性。
- 数据集划分：local_v1 前 4（dev，打通链路用，不作最终结论）/ 后 2（holdout，策略冻结后跑）。
- 公平基线：B0（关掉分层治理）/ B1（开 Context Engine v2）/ B2（LangGraph ReAct 同工具同步数）/ B3（一次性生成补丁，不迭代）。
- 主实验 B0 vs B1 vs B2；消融 A1–A6（关 Artifact / 关陈旧清理 / 关 Microcompact / 关全量摘要 / 关 Prompt Cache 热前缀 / 关子Agent Reviewer）；鲁棒性实验（在第 N 次 LLM/工具调用注入故障、取消任务验证快照配对）。
- 日志契约：每次运行记 Git commit、数据集版本、case_id、模型、Provider、种子、最大步数、上下文策略、起止时间、事件文件、最终补丁、公开/隐藏测试输出。

### 结果表与图（`tables/`、`figures/`）
- 4 张表：`main-resolution`（主基线比较）、`context-ablation`（消融）、`robustness`（故障恢复）、`category-breakdown`（分类别能力）。数值报 `mean ± std` + Wilson 95% 置信区间。
- `figures/data-manifest.md` 明确：**尚无真实 Agent 运行结果，不生成结果图**；模拟数据必须以 `mock_`/`synthetic_` 前缀，不得写入最终结论。

### 防失真规则（`README.md`，面试必提）
- Agent 只能看到 `seed/` 工作区 + `TASK.md`，不能把 `oracle/`/`solution/` 带进工作区。
- 同模型/Prompt/工具权限/最大步数/上下文策略 **至少重复 3 次**。
- 必须保留原始事件、最终补丁、测试输出、Token 用量——**不只记成功率**。
- `local_v1` 是开发集；调参后必须用未参与调试的开源任务验证泛化。
- 合成任务结果**不能**宣称为 SWE-bench 或真实生产任务成绩。

---

## 一句话定位（面试怎么说，不夸大）

> "我们建了一套分层的评测：一层是**确定性的 Runtime 机制评测**（`system_eval.py`，覆盖上下文治理、记忆召回、崩溃恢复，11 个 case 全过），用合成 fixture 把机制正确性锁死；另一层是**真实编码任务评测**（`harness.py` + local_v1/QuixBugs/SWE-bench 分层数据集），通过事件重放（`core/eval`）把每次 run 的轨迹指标化，再配合故障注入做鲁棒性实验。目前机制层已实跑通过，任务层框架和数据分层已就绪、正按 dev/holdout 协议推进真实运行。"

## 必须主动说明的能力边界（别被追问打脸）
1. `system_eval` 验证的是 **Runtime 机制**，不等价于"真实模型的代码任务成功率"，**不能拿它宣称 SWE-bench 成绩**。
2. `core/eval` 是**离线轨迹重放**，分析已发生的 run，本身是事后度量，不含决策。
3. `methods—experiment-traceability` 矩阵显示：多数主实验/消融/鲁棒性实验状态为**"协议已定义 / 待真实运行 / 本地数据集建设中"**——也就是说"实验设计完整，端到端真实跑分还在路上"。这是诚实且加分的说法。

## 高频追问与应答要点
- **"为什么不直接用 SWE-bench 当唯一指标？"** → 成本与隔离环境门槛高、迭代慢；先用确定性合成 fixture 锁死机制正确性，SWE-bench 只放最终外部效度验证位。
- **"轨迹重放 vs 重新执行，区别在哪？"** → 重放只读 `events.jsonl` 聚合指标，不重新调模型/工具，可重复、可离线、零成本；重新执行才验证行为本身。
- **"故障注入怎么设计？"** → `FaultSpec` 把故障钉在第 N 次调用（LLM 或工具），kind 覆盖 timeout/connection/rate_limit/runtime，用来验证重试策略、流式降级、原子恢复真的生效。
- **"10 万字符结果外置，你怎么保证不丢信息？"** → `sha256` 校验：外置文件 + 上下文里只留引用，还原后字节级一致，且 `is_error/error_type` 语义保留。
