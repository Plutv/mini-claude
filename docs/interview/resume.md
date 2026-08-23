# 简历定稿 — 本地智能编码 Agent｜核心开发

> 项目日期：2026年5月—至今  
> 技术栈：Python、asyncio、Pydantic、Anthropic SDK、httpx、JSON-RPC、MCP、pytest

---

**本地智能编码 Agent｜核心开发**　　　　　　　　　　　　**2026年5月—至今**
技术栈：Python、asyncio、Pydantic、Anthropic SDK、httpx、JSON-RPC、MCP、pytest

- 针对终端退出导致长任务中断的问题，设计独立 Core Runtime 与 CLI/TUI 客户端架构，基于 TCP + NDJSON 实现 JSON-RPC 命令调用与异步事件推送；将 Agent Run 作为后台任务持续执行，并通过 Session 恢复、运行事件持久化及按 `run_id/topic` 回放实现客户端断线重连。
- 实现基于 ReAct 的 Agent Loop 与声明式工具调度机制，统一管理本地及 MCP 工具；根据 `parallel_safe` 元数据将只读工具分批并行、具有副作用的工具串行执行，并结合异步权限审批、指数退避重试、超时控制和取消传播约束工具执行边界。
- 面向长任务中工具输出与历史消息持续膨胀的问题，设计四层 Context Engine：将超大工具结果外置为 Artifact，按上下文水位清理陈旧结果、执行局部 Microcompact，并在高水位触发 LLM 摘要；通过候选上下文隔离、工具消息配对校验及 Step 边界原子快照，保证压缩失败可回退且后续会话能够加载「摘要 + 未压缩尾部」。
- 抽象统一 LLM Provider 接口，适配 Anthropic 与 OpenAI-compatible 的消息、工具调用和流式响应协议；设计基于请求复杂度与 Provider 健康状态的模型路由、熔断及降级机制，仅在首个可见 Token 产生前切换备用模型，避免流式中断造成多模型残缺输出拼接。
- 构建面向 Agent 执行轨迹的评测与故障注入框架，从事件日志聚合任务状态、工具成功率、重试次数、上下文压缩、Token 消耗及执行延迟，并模拟模型限流、网络异常、工具超时和任务取消，验证关键链路的容错行为。

---

## 一句话卖点

> 我实现的是一个面向本地编码任务的 Agent Harness，重点不是聊天界面，而是解决长任务执行中的工具调度、权限控制、上下文膨胀、多模型流式降级和执行轨迹评测问题。

## 当前限制（面试主动说明）

1. Core 不是 systemd 级真正守护进程；
2. Checkpoint 不是任意位置断点续跑；
3. 不保证外部工具副作用 exactly-once；
4. 并发安全依赖工具的 `parallel_safe` 声明；
5. 事件回放不等于重新执行 Agent；
6. Provider 降级仅限未产生可见 Token；
7. 故障注入目前主要验证确定性异常，不是线上混沌工程。

## 亮点与代码映射

### 亮点一：长任务和客户端生命周期解耦
- `asyncio.create_task()` 启动后台任务：[app.py:118](/e:/project_2026/python/KamaClaude/src/kama_claude/core/app.py:118)
- 事件回放重连：[app.py:204](/e:/project_2026/python/KamaClaude/src/kama_claude/core/app.py:204)

### 亮点二：声明式工具调度
- `parallel_safe` 扫描 + batch 切分：`core/loop.py:79`

### 亮点三：四层 Context Engine
- 核心入口：`core/compact/engine.py:105`

### 亮点四：流式安全模型降级
- Router 无可见 Token 才切换：`core/llm/router.py:88`

### 亮点五：轨迹评测与故障注入
- 轨迹聚合：`core/eval/trajectory.py:49`

## 版本历史
- v2.0（当前）：增加攻击性表达，五大亮点与代码映射，一句话卖点，当前限制。
- v1.0：面试安全版，保守准确但偏审计报告风格。