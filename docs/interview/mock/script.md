# 模拟面试脚本 — 面试官视角

## 面试设定
- 岗位：AI Agent 后端开发 / 大模型应用工程师
- 时长：约 30-40 分钟
- 风格：连环追问，一层层深入，遇到模糊回答会打断追问

---

## 开场（2分钟）

> "先花一分钟介绍一下自己吧，然后重点讲讲你简历上这个 Agent Harness 项目——不用面面俱到，挑一个你觉得最有技术含量的模块切入。"

---

## 模块一：Agent Loop 与工具调度（8-10分钟）

### Round 1：事实确认
**Q1.1**："你简历里提到基于 `parallel_safe` 做工具调度，我想确认一个细节——这个标记是谁来决定的？"

### Round 2：设计追问（如果答"工具开发者显式声明"）
**Q1.2**："如果我新写了一个工具，比如 `git_status`，读仓库状态不修改文件，我会把它标记成 `parallel_safe=True`。但如果另一个开发者同时写了 `git_commit`，他也标记了 `True`，并行执行两个 commit 会发生什么？"

### Round 3：边界与修复
**Q1.3**："所以你们目前完全依赖开发者的自觉性。有没有考虑过运行时自动检测？比如通过文件系统访问追踪或者沙箱？"

### Round 4：错误传播
**Q1.4**："回到 `invoke_tool`，你说它永不抛异常，所有失败都包装成 `ToolResult(is_error=True)`。那 `CancelledError` 呢？你代码里 `except Exception` 能抓到 `CancelledError` 吗？"

### Round 5：实战场景
**Q1.5**："假设现在一个 Run 里有三个工具调用：`read_file(A)`、`read_file(B)`、`write_file(C)`。`read_file` 是 parallel_safe，`write_file` 不是。模型一次返回了这三个 tool_use，按你的 batch 算法，实际执行顺序和并发度是怎样的？如果 `read_file(A)` 超时了，会影响 `read_file(B)` 和 `write_file(C)` 吗？"

---

## 模块二：Context Engine（8-10分钟）

### Round 1：概念澄清
**Q2.1**："你提到四层 Context Engine，Artifact 外置、陈旧清理、Microcompact、全量摘要。我想先问第一层——Artifact 外置。一个工具结果多大时会被外置？这个阈值是固定的吗？"

### Round 2：深入 Microcompact
**Q2.2**："第三层 Microcompact 的触发条件是 `utilization >= 70%` 并且满足 `idle > 300s` 或 `step % 4 == 0`。为什么加了这两个额外条件？如果去掉 `step % 4 == 0`，只在 idle 时触发，有什么问题？"

### Round 3：压缩回退
**Q2.3**："全量摘要失败时你用 deepcopy 回退。但 deepcopy 在消息历史很长时本身也可能很慢甚至 OOM，你们实际遇到过这个问题吗？如果 deepcopy 也失败了怎么办？"

### Round 4：效果验证
**Q2.4**："你们做摘要的核心目标是降低 Token 消耗，但还有一个隐性成本——摘要质量不够高会导致后续任务成功率下降。你们怎么验证'压缩后任务成功率没有显著下降'？"

### Round 5：协议安全
**Q2.5**："你提到 `tool_pairs_balanced()` 检查。如果我手动构造了一个 messages 列表，里面有 tool_use 但没 tool_result，你的 engine 会怎么处理？SessionStore 加载时又会怎么处理？"

---

## 模块三：Provider 与流式降级（6-8分钟）

### Round 1：协议差异
**Q3.1**："Anthropic 和 OpenAI 的工具调用格式差异很大。你代码里 `_convert_messages` 做了格式转换。如果模型返回了 `thinking` block，OpenAI provider 会怎么处理？"

### Round 2：流式核心
**Q3.2**："OpenAI 的 tool_calls 在 SSE 里是分片到达的，你用 `partial_calls` 按 index 累加。如果模型一次返回了两个 tool_call，index=0 和 index=1，但 index=0 的 arguments 分了三片才传完，index=1 的 id 在第一片就到了——你的累加逻辑能保证最后组装出的两个 tool_call 都是完整的吗？"

### Round 3：降级边界（最关键）
**Q3.3**："ProviderRouter 里 `_ForwardingBus.token_count` 决定是否降级。假设主模型已经输出了一万个 token，然后网络断开，Router 会怎么做？为什么不能切备用模型？"

### Round 4：熔断策略
**Q3.4**："熔断状态存在内存里，进程重启后重置。如果你们的 Core 进程频繁重启（比如内存泄漏被 OOM killer 杀掉），熔断还有意义吗？"

---

## 模块四：Core 进程与事件回放（5-6分钟）

### Round 1：进程分离
**Q4.1**："你们用 TCP + NDJSON 做 IPC，事件回放从 `events.jsonl` 读取。如果客户端断开 10 分钟后重连，这 10 分钟里产生的事件他怎么拿到？"

### Round 2：回放语义
**Q4.2**："事件回放等于重新执行 Agent 吗？如果回放期间用户看到'某步 read_file 返回了旧内容'，但实际上那个文件已经被后面的 step 改掉了，用户会不会被误导？"

### Round 3：Session 恢复
**Q4.3**："Core 重启时，你们把 running 状态的 session 标记为 interrupted。如果重启前那个 Run 只差最后一步就 end_turn 了，用户恢复后需要从头来吗？"

---

## 系统设计题（5分钟）

**Q5**："假设你们要在现有架构上支持'多个用户同时观察同一个 Run 的进度'，比如一个开发者在跑代码审查 Agent，他的主管想连上来看实时进度。现有架构支持吗？如果不支持，最少改动哪里？"

---

## 面试结束

> "好的，技术问题就到这。最后问你一个开放的：如果给你两周时间，只改一个模块，你会选哪个？为什么？"

---

## 评分维度（面试官内部用）

| 维度 | 权重 | 考察点 |
|------|------|--------|
| 事实准确性 | 30% | 代码细节是否说对，有没有夸大 |
| 设计深度 | 30% | 是否理解 trade-off，不是只背代码 |
| 边界意识 | 20% | 异常路径、极端场景是否考虑 |
| 表达能力 | 20% | 是否结构化、是否有重点、是否啰嗦 |

## 高风险回答（会扣分）

- "系统自动识别工具的副作用" → 事实是显式声明
- "超时后会重试" → 事实是不重试
- "Checkpoint 保证 exactly-once" → 事实是只保证消息原子性
- "事件回放等于重新执行" → 事实是只读历史展示
- "流式中断后可以切模型" → 事实是 token_count>0 时不再降级
- "摘要失败会破坏历史" → 事实是 deepcopy 回退