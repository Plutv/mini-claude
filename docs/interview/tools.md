# KamaClaude 工具系统 — 面试问答准备

> 一句话架构：**所有工具都实现同一个 `BaseTool` 抽象，由统一的 `invoke_tool` 包装器执行（校验→权限→超时→永不抛异常→可重试），注册进 `ToolRegistry`，Agent Loop 按 `parallel_safe` 标记把可并行工具用 `asyncio.gather` 并发调度。**

---

## 0. 先讲清楚的整体结构（开场 30 秒版）

"我们的工具分三层：
1. **抽象层**——`BaseTool`（name/description/input_schema/parallel_safe/invoke）+ `ToolResult(content, is_error, error_type)`。
2. **执行层**——`invoke_tool` 统一包装每次调用：Pydantic 参数校验、Plan 守卫、权限检查、超时、Artifact 外置、**永不抛异常**、只对 `runtime_error/rate_limited` 指数退避重试。
3. **注册与调度层**——`ToolRegistry` 按名查工具并产出 Anthropic 格式 schema；Agent Loop 的 `_invoke_requested_tools` 把连续的 `parallel_safe` 工具用 `asyncio.gather` 并发跑，同时**保持模型给出的顺序**。"

---

## 1. 基础契约（必考）

### `BaseTool`（base.py）
- 字段：`name`、`description`、`input_schema`（Anthropic 格式 dict）、`params_model`（Pydantic，可选）、`parallel_safe: bool = False`。
- 唯一方法：`async def invoke(params) -> ToolResult`。
- **`parallel_safe` 是关键设计**：默认 `False`。只有声明"无副作用、可并发"的工具才置 `True`。当前内置工具里 `read_file`、`grep_search`、`git_diff` 是 `True`（均只读）；`write_file`/`bash`/`list_dir`/`apply_patch`/`run_tests` 都有副作用或不确定结果，必须串行。

### `ToolResult`（base.py）
- `content: str`、`is_error: bool`、`error_type: str | None`。
- `error_type` 取值约定：`runtime_error` / `timeout` / `schema_error` / `permission_denied` / `conflict` / `rate_limited` / `plan_mode_denied` / `action_budget_exceeded`。
- **error_type 决定两件事**：① 是否可重试；② 上下文里怎么呈现错误语义。

---

## 2. 执行包装器 `invoke_tool`（invocation.py）— 最核心的实现

调用链路（按顺序）：
1. 发 `ToolCallStartedEvent`。
2. **未知工具** → `runtime_error`。
3. **Plan 守卫**：若处于 plan 模式，非只读工具被 `plan_mode_denied` 拦截。
4. **参数校验**：`params_model.model_validate` 失败 → `schema_error`（**不重试**）。
5. **权限检查**：`permission_manager.check_and_wait`；拒绝 → `permission_denied`（**不重试**），返回引导文案"Try an alternative approach"。
6. **行动预算**：plan 模式二次守卫 `action_budget_exceeded`。
7. **执行**：`asyncio.wait_for(tool.invoke(...), timeout)`，默认 120s。
8. 成功且非错误 → 可选 `artifact_store.externalize`（超阈值外置），发 `ToolCallFinishedEvent`，返回。
9. 失败分支：
   - `RateLimitedError` → `rate_limited`；`TimeoutError` → `timeout`；其他 `Exception` → `runtime_error`。
   - **重试判定**：`error_type in {runtime_error, rate_limited}` 且 `attempt <= 2` → 发 `ToolCallFailedEvent` + 指数退避 `2s * 2**(attempt-1)` 后重试；否则 `_fail` 返回。
10. **`timeout` / `schema_error` / `permission_denied` / `conflict` 一律不重试。**

> 关键不变量：**`invoke_tool` 永不向 Agent Loop 抛异常**。所有失败都变成 `ToolResult(is_error=True)` 回填，`CancelledError` 才需要向上传播（客户端断网取消）。这正是 Agent 能"自愈"而不是整个 Run 崩掉的根本。

---

## 3. 工具清单（作用 + 实现）

### A. 文件 IO（核心三件套）
| 工具 | 作用 | 实现要点 |
|---|---|---|
| `read_file` | 读取文本文件内容 | `asyncio.to_thread(path.read_bytes)`；>512KB 截断；`..` 路径穿越拦截；读时把 SHA256 记入 `FileVersionTracker`（为写冲突检测做准备）；**`parallel_safe=True`** |
| `write_file` | 写/覆盖/创建文件 | 1MB 上限；`..` 拦截；自动建父目录；**写冲突守卫**——未先读或读取后被改过则 `conflict` 拒绝（见 §4.2） |
| `list_dir` | 树状列出目录 | 深度 ≤4、条目 ≤200；`..` 拦截；排序（目录在前） |
| `bash` | 执行 shell 命令 | `asyncio.create_subprocess_shell`，stdout+stderr 合并；64KB 截断；默认超时 60s（≤120s）；非零退出码 → `runtime_error`；明确"非交互、要输入会卡死超时" |

### B. 任务追踪（4 个，基于 `TaskManager`）
- `task_create` / `task_get` / `task_list` / `task_update`：让 Agent 把大目标拆成可跟踪的小任务，支持 `blocked_by` 依赖。状态 `pending/in_progress/completed`，完成时自动从其他任务的 blocked_by 清除。底层 `.tasks` 目录持久化。
- 用途：长任务的可观测性与自组织。

### C. 长期记忆写入
- `note_save`：把" durable fact / 用户偏好 / 项目决策"写入记忆。scope ∈ `session/project/global`；经 `MemoryStore` 落库（带 tag、importance、source_run_id）。**这是记忆系统的写入口**，配合 `system_eval` 里验证的 scope 隔离与召回。

### D. 编排 / 子 Agent（3 个）
- `spawn_agent`：派生子 Agent，**冷上下文启动（不继承父对话历史）**；`depth` 从 0 起，最大嵌套 2；支持前台阻塞 / 后台并行（`run_in_background`）；子 bus 事件桥接回父 bus 供 TUI 渲染嵌套进度；按角色 profile + `allowed_tools` 过滤子 registry（子 Agent 只拿到 read_file/bash/write_file/list_dir + 其任务工具，深度<1 才允许再 spawn / agent_result / cancel_agent）。
- `agent_result`：轮询后台子 Agent 状态/结果（`wait` + `timeout_s`）。
- `cancel_agent`：按 run_id 取消后台子 Agent。

### E. 计划模式（3 个，基于 `PlanController`）
- `enter_plan_mode`：进入只读规划；`update_plan`：写计划但不执行；`request_execution`：请求人工批准后切回执行模式。
- 实现上是"先想后做"的安全闸——规划阶段任何会改状态的工具都被 `invoke_tool` 里的 plan 守卫挡掉。

### F. 技能
- `skill`：调用被发现的 skill，返回其解析后的指令。若 skill `context=="fork"`，复用 `spawn_agent` 在子上下文里执行（即"技能 = 带 prompt 的一次子 Agent 派发"）。

### G. 外部 MCP（动态）
- `McpTool`：把任意 MCP server 的工具适配进本地 `ToolRegistry`，命名为 `{server}__{tool}`；`parallel_safe = tool_def.read_only_hint`；调用异常统一降级为 `ToolResult(is_error=True)`（server 不可用 / 工具报错 / 意外错误三类）。

### H. 编码主闭环（4 个，组成 Locate→Read→Patch→Verify→Review）
把"改代码"做成一条可面试讲清楚的专业闭环：
| 工具 | 作用 | 实现要点 |
|---|---|---|
| `grep_search` | 在 workspace 内按正则定位代码（Locate） | 优先 `rg --json`，无 rg 回退纯 Python；返回结构化匹配（path/line/text/is_match）；`parallel_safe=True`（只读） |
| `apply_patch` | 按统一 diff 或 search/replace 改文件（Patch） | `_apply_unified_diff` 解析 hunk + `edits` 模式；接 `FileVersionTracker` 冲突检测与原子写盘；返回 applied/failed hunk 数 |
| `run_tests` | 跑测试验证改动（Verify） | asyncio 子进程跑命令（默认 pytest），纯函数解析 passed/failed；返回结构化 summary |
| `git_diff` | 出 diff 并做安全审查（Review） | 调 `git diff [--staged]`；启发式查泄露密钥 / 残留断点 / 大范围纯空白改动 / 无关文件过多 |

- 四个工具全部复用 `BaseTool` + `invoke_tool` 框架，注册进 `runner._build_registry`（带 `_ok` 白名单守卫）。
- 面试讲法：感知（grep）→ 修改（apply_patch）→ 验证（run_tests）→ 控制（git_diff 审查），比"整文件覆盖 + bash 跑测试"更专业、更可控。

---

## 4. 三个贯穿性的实现亮点（追问时主动讲）

### 4.1 并行调度（`loop.py:_invoke_requested_tools`）
- 模型一次可能返回多个 `tool_calls`。实现：**扫描连续的可并行段**，段内用 `asyncio.gather` 并发，段间保持顺序；遇到 `parallel_safe=False` 或未知工具就先串行跑它再继续。
- 为什么只 `read_file` 能并行？因为它只读、无副作用、结果彼此独立；`bash`/`write_file` 会改变工作区或依赖前序状态，并发会引入竞态。

### 4.2 写冲突守卫（`file_versions.py` + `write_file`）
- `FileVersionTracker` 在 `read_file` 时记录文件 SHA256；`write_file` 写入前 `validate_write`：若文件存在且①从未被读过 或 ②读取后被改过 → 返回 `conflict` 拒绝覆盖。
- 这是"乐观并发控制"的轻量版，防止 Agent 在没看文件的情况下盲目覆盖、或覆盖别人（含自身别的分支）改过的文件。

### 4.3 大结果外置（`artifacts.py`）
- `ToolArtifactStore.externalize`：结果 > 阈值（默认 32KB）就写到磁盘文件（`.txt` + `.json` 元数据含 sha256/is_error/error_type），上下文里只留 `head(2KB)+...[omitted]...+tail(2KB)` 预览 + 路径 + sha256。
- 好处：① 保护上下文预算；② 字节级可逆（sha256 校验，需要细节时 `read_file` 回看）；③ 错误语义（`is_error`/`error_type`）在引用里保留。

---

## 5. 面试"怎么答"脚本 + 高频追问

**开场（被问"你这个项目有哪些工具 / 工具是怎么设计的"）：**
> "工具整体分内置核心工具（文件 IO、任务追踪、记忆写入）和编排类工具（子 Agent、计划模式、技能、外部 MCP）。它们都实现同一个 `BaseTool` 抽象，由 `invoke_tool` 统一执行——核心是：参数用 Pydantic 校验、权限在工具外统一拦截、超时可控、失败**绝不抛异常而是回填 ToolResult**、只对 `runtime_error/rate_limited` 做指数退避重试。Agent Loop 再根据 `parallel_safe` 把只读工具并发调度。我们还做了三件保障安全与效率的事：写冲突守卫、大结果外置、路径穿越拦截。"

**高频追问 + 应答：**
- **"工具调用失败会怎样？会不会让整个 Agent 挂掉？"** → 不会。`invoke_tool` 把任何异常包成 `ToolResult(is_error=True)` 回填给模型，模型可以自我纠正；只有取消信号（断网）需要传播。
- **"哪些工具能并行？为什么不是全部？"** → 只有声明 `parallel_safe` 的 `read_file`。因为 `bash`/`write_file` 有副作用且可能依赖前序结果，并发会竞态。调度器专挑"连续的可并行段"用 gather 并发，其余串行且保序。
- **"如果模型把文件覆盖了怎么办？"** → `FileVersionTracker` 乐观锁：写前必须读过且未被改过，否则 `conflict` 拒绝，模型会改去先 read 再写。
- **"10 万行日志塞进上下文怎么办？"** → `ToolArtifactStore` 外置到磁盘，上下文只留 sha256 + 预览，需要细节时 `read_file` 回看，字节级一致。
- **"怎么保证 Agent 不乱改文件 / 不乱执行命令？"** → 两层：① 权限管理器在 `invoke_tool` 里统一拦截并等用户确认；② plan 模式让"改状态"的工具在规划阶段全部被拦。
- **"子 Agent 会不会无限套娃？"** → 不会，`depth` 上限 2，到达后 spawn 直接返回错误；子 Agent 工具集也按角色裁剪，不是全量继承。
- **"工具 schema 怎么给模型的？"** → `ToolRegistry.tool_schemas()` 直接产出 Anthropic 格式 `{name, description, input_schema}` 列表；MCP 工具动态加入时同名覆盖。
- **"怎么加一个新工具？"** → 继承 `BaseTool`，填 name/description/input_schema，写 `async def invoke`，声明 `params_model` 和 `parallel_safe`，`registry.register` 即可；若需副作用安全可设 `parallel_safe=True`。

**一句话收尾：** "工具系统是我对'Agent 可靠性'最直接的落地——把校验、权限、超时、重试、外置、冲突检测全部收口在统一执行层，让单个工具的失败不影响整个 Run，也让并发和长上下文这两件事变得可控。"
