# Core / Session / 事件回放面试卡 — 简历第一点

> 对应代码：`src/kama_claude/core/app.py`、`core/transport/socket_client.py`、`core/transport/ipc_broadcaster.py`、`core/session/manager.py`、`core/session/store.py`、`core/runner.py`。  
> 真实实验参考：`tests/unit/test_socket_server.py`、`test_socket_client.py`、`test_ipc_broadcaster.py`、`test_session_manager.py`、`test_session_store.py`。

---

## 1. 解决的问题
- CLI/TUI 客户端生命周期短（用户随时关终端），但 Agent Run 可能持续数分钟。
- 客户端断开后重连，需要"补齐"中间状态（不是从头再跑）。
- 多个客户端可能同时连接 Core，需要事件分发和隔离。

## 2. 最初方案
单进程：CLI 直接调 LLM API，没有后台任务概念。客户端退出 = 任务终止。

## 3. 暴露的问题
- 长任务绑定到终端生命周期 → 用户离开电脑回来发现任务中断了。
- 没有持久化 → 重启后所有会话历史丢失。
- 没有事件机制 → 前端不知道 Agent 执行到哪一步了。

## 4. 最终设计

### 架构：独立 Core 服务进程 + 客户端
```
┌─────────────┐     TCP + NDJSON      ┌─────────────────────────────┐
│  kama (CLI) │  ←────────────────→   │         kama-core           │
│ kama-tui    │   JSON-RPC 命令响应   │  ┌─────────────────────┐    │
│             │   EventPush 事件推送   │  │   SocketServer      │    │
└─────────────┘                       │  │  (asyncio.start_server)│  │
                                      │  └─────────────────────┘    │
                                      │         ↓ register handlers │
                                      │  ┌─────────────────────┐    │
                                      │  │      CoreApp        │    │
                                      │  │  · _running_runs    │    │
                                      │  │  · SessionManager   │    │
                                      │  │  · EventBus         │    │
                                      │  │  · IpcBroadcaster   │    │
                                      │  └─────────────────────┘    │
                                      └─────────────────────────────┘
```

### 三层通信语义

| 语义 | 方向 | 格式 | 用途 |
|------|------|------|------|
| **RPC 命令** | client → core | `{"jsonrpc": "2.0", "id": "...", "method": "...", "params": {...}}` | 发命令（"开始 run"），返回"命令是否被接受" |
| **RPC 响应** | core → client | `{"jsonrpc": "2.0", "id": "...", "result": {...}}` | 命令执行结果（如 `run_id`） |
| **事件推送** | core → client | `{"kind": "event", "event": {"type": "...", ...}}` | 持续展示 Agent 执行过程（token、工具调用、step 等） |

- **RPC 是请求-响应**：发 `session.send_message` → 收到 `SessionSendMessageResult`（含 `run_id`）。
- **事件是单向推送**：Run 开始后，core 持续发 `step.started`、`llm.token`、`tool.call_started` 等事件，client 被动消费。

### 后台任务与客户端解耦
- `_agent_run_handler`（`app.py:107`）：收到 `agent.run` 命令后，`asyncio.create_task()` 启动后台任务，立即返回 `run_id`。
- `_running_runs: set[asyncio.Task]` 跟踪所有活跃 Run（`app.py:73`）。
- 客户端 TCP 断开 → `SocketClient.run_event_loop` 的 readline 收到空字节 → 退出循环 → `cancel()` 所有 pending futures。但**后台 Run task 不受影响**——因为它在 core 进程里独立运行。

### Session 恢复
- `session.resume`（`app.py:151`）：从 `SessionStore` 读取 `meta.json` 和 `thread.jsonl`，重建 session 状态和消息历史。
- `SessionManager._restore_index()`（`manager.py:57`）：core 启动时扫描所有 session，把状态为 `active/running` 的标记为 `interrupted`（daemon_restarted）——诚实告知用户"上次 core 挂了"。
- 恢复后 session 状态变为 `waiting_for_input`，用户可以发新消息继续。

### 事件回放
- `event.subscribe`（`app.py:200`）：客户端订阅时可指定 `replay_from_run=run_id`。
- `_replay_events()`（`app.py:215`）：从 `runs/<run_id>/events.jsonl` 读取历史事件行，按 topic glob 匹配（`fnmatch`），通过 writer 推送给客户端。
- 回放完历史事件后，再注册实时订阅（`IpcBroadcaster.subscribe`）。
- **关键**：回放的是"已持久化的事件"，不是重新执行 Agent——文件系统状态可能已经变了。

### 事件持久化
- `EventWriter`（`runner.py:273`）：每个 Run 有自己的 `events.jsonl`，`async with` 上下文管理器保证文件正确关闭。
- `EventBus` → `EventWriter` 订阅 → 所有事件（`RunStarted`、`StepStarted`、`ToolCallStarted` 等）自动写入 JSONL。
- `IpcBroadcaster`（`ipc_broadcaster.py`）：另一个 `EventBus` 订阅者，负责把事件推送给所有匹配的 TCP 客户端。

### 事件订阅与过滤
- `IpcBroadcaster.subscribe(writer, topics, scope)`：topics 支持 `fnmatch` glob（如 `run.*`、`step.*`、`tool.*`）；scope 可以是 `global`（全通）或 `run:<id>`（只推该 run 的事件）。
- 写入失败（`ConnectionResetError`、`BrokenPipeError`）时自动 `unsubscribe` 死连接。

### Checkpoint：Session 消息原子保存
- `AgentLoop` 的 `checkpoint` 回调（`runner.py:329`）：在 step 边界调用 `store.write_messages_atomic(session.id, messages)`。
- `write_messages_atomic`（`store.py:150`）：
  1. `tool_pairs_balanced(messages)` 检查——不平衡时拒绝写入。
  2. 写 tmp 文件 + `fsync` + `os.replace`——保证不半写。
- 只在完整 step 边界 checkpoint，daemon crash 不会留下孤儿 tool_use。

## 5. 为什么这样取舍

### 为什么分离进程？
- Agent Run 可能持续数分钟，不能绑定到 TUI 生命周期。
- 客户端只负责发命令和消费事件，执行状态由 Core 维护。
- 断开后任务继续，重连时补齐中间状态。

### 为什么用 TCP + NDJSON 而不是 Unix Domain Socket？
- 代码中注释说 Unix domain socket，但实际用 TCP loopback（`127.0.0.1:7437`）。
- TCP 跨平台（Windows 也支持），且便于调试（可以用 `nc`/`telnet` 直接连）。
- NDJSON（每行一个 JSON）天然适合流式读取：`readline()` 一次读一行。

### 为什么 RPC 响应和事件推送共用一条 TCP 连接？
- 简化连接管理：一个客户端只需要一条 TCP 连接。
- `SocketClient._dispatch()`（`socket_client.py:85`）通过 `"jsonrpc" in msg` 区分 RPC 响应和事件推送。

### 为什么事件回放不是重新执行 Agent？
- 重新执行意味着重新调工具，可能产生副作用（如 `write_file` 再次写入）。
- 回放只是"给客户端看之前发生过什么"——TUI 用这些事件重建进度条和日志视图。
- 外部状态（文件系统）可能已经变了，所以回放后客户端看到的"历史"不一定能复现。

### 为什么 session 恢复时要标记 interrupted？
- core 重启时，正在跑的 Run 被强制终止（`SIGTERM` handler 里 `cancel()` 所有 task）。
- 诚实地标记 `interrupted_reason = "daemon_restarted"`，让用户知道"上次 core 挂了，不是任务正常结束"。
- 这是"不假装一切都好"的设计哲学。

## 6. 当前限制
- **不是 systemd 级守护进程**：core 启动后确实持续运行，但没有注册成 OS 服务——终端关了就关。
- **Checkpoint 不是任意位置断点续跑**：只在 step 边界快照，step 中途 crash 会丢失该 step 的进度。
- **不保证外部副作用 exactly-once**：bash 命令已经执行了，但 checkpoint 在之后——crash 后重跑会再执行。
- **事件回放 ≠ 重新执行**：回放只展示历史，不恢复外部状态。
- **单节点**：没有多进程/多机扩展能力。

## 7. 如何测试

| 测试文件 | 验证什么 |
|---------|----------|
| `test_socket_server.py` | TCP 服务器启动、handler 注册、NDJSON 消息解析、并发连接 |
| `test_socket_client.py` | 连接、命令发送、响应接收、事件回调、连接断开清理 |
| `test_ipc_broadcaster.py` | 订阅注册、topic glob 匹配、scope 过滤、死连接清理 |
| `test_session_manager.py` | session 创建、恢复、关闭、消息发送、状态流转 |
| `test_session_store.py` | meta.json 读写、thread.jsonl 追加/读取、atomic 写入、orphan tool_use 裁剪 |
| `test_agent_run_subscription.py` | agent.run 命令后的事件订阅和回放 |

## 8. 后续如何升级
- **systemd/Windows Service 包装**：让 core 真正成为 OS 级守护进程。
- **增量 Checkpoint**：对工具中间状态（如 bash 子进程 PID）做快照，恢复时能续跑而不是从头。
- **多客户端同步**：当前一个 session 同时只能有一个 Run（`asyncio.Lock`），未来可支持只读客户端并发观察。
- **WebSocket 替代 TCP**：支持浏览器客户端连接。

---

## 4 个核心问题速答

### Q1：RPC 响应和事件推送的关系？
- **RPC 响应** = "命令是否被接受"（如 `agent.run` → `run_id`）。这是请求-响应模式。
- **事件推送** = "Agent 执行过程中的实时进度"（`step.started`、`llm.token`、`tool.call_finished`）。这是单向流。
- 两者共用一条 TCP 连接，通过 `"jsonrpc" in msg` 区分。

### Q2：客户端断开后任务为什么能继续？
- `agent.run` 命令触发 `asyncio.create_task()` 启动后台 Run（`app.py:118`）。
- `_running_runs: set[asyncio.Task]` 跟踪所有活跃 Run。
- 客户端 TCP 断开只影响该连接的 `SocketClient`——core 进程里的 Run task 不受影响。
- 重连后通过 `event.subscribe(replay_from_run=run_id)` 回放历史事件补齐进度。

### Q3：事件回放如何保证时序？
- 事件按产生顺序写入 `runs/<run_id>/events.jsonl`（每行一个 JSON）。
- `_replay_events()` 按行顺序读取，通过 `fnmatch` 匹配 topic，原序推送给客户端。
- 回放完历史后，再注册实时订阅——客户端看到的是"历史 + 实时"无缝衔接。

### Q4：Session 恢复时加载了什么？
- `meta.json`：session 元数据（id、mode、status、title、run_ids、interrupted_reason）。
- `thread.jsonl`：完整的 Anthropic messages 历史（`role` + `content`）。
- `notes.md`：用户主动保存的笔记（通过 `note_save` 工具）。
- 恢复后 `ExecutionContext.prefill_messages = history`，新 Run 从上次历史继续。