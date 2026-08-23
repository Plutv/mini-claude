# Workspace 隔离与结构化编码工具

## 问题

Core 与 CLI/TUI 分进程后，不能再隐式使用 Core 的启动目录作为所有任务的工作目录。否则客户端即使从另一个仓库发起任务，文件工具仍可能读写错误的项目；只检查路径中是否包含 `..` 也无法阻止绝对路径和符号链接逃逸。

## Session 绑定 Workspace

客户端创建 Session 时把当前绝对目录作为 `workspace` 发送给 Core。Core 规范化并校验目录后写入 `meta.json`，后续 Run、会话恢复和子 Agent 都继承该值。

这带来三个性质：

1. Workspace 生命周期与 Session 一致，不依赖 Core 从哪里启动；
2. 恢复旧会话时不会因为当前终端目录变化而切换项目；
3. 父子 Agent 使用相同项目边界，避免子任务意外操作其他仓库。

旧版 Session 没有 `workspace` 字段，Core 建立索引时会用当前目录补齐并重新持久化，保证元数据向后兼容。

## 文件工具边界

`Workspace.resolve()` 同时处理相对路径、绝对路径和符号链接：候选路径经过 `resolve()` 后必须仍位于 Workspace 根目录，否则抛出 `PermissionError`。

该边界覆盖 `read_file`、`write_file`、`edit_file`、`search_text` 和 `list_dir`。`bash` 只把子进程工作目录设置为 Workspace，并继续经过权限审批；它不是容器或系统调用级沙箱，不能宣称完全阻止命令访问外部路径。

## read_only 与 parallel_safe 分离

工具是否只读和工具是否能并发是两个独立问题：

- `read_only` 决定 Plan Mode 能否调用，以及是否消耗副作用预算；
- `parallel_safe` 决定同一轮的多个调用能否通过 `asyncio.gather` 并行执行。

例如远程 MCP 工具可能声明只读，但 MCP Server 未必支持并发，因此可以是 `read_only=True, parallel_safe=False`。旧实现把两者共用一个字段，会导致 Plan Mode 错误阻止串行只读工具，或者错误并发调用远程服务。

## 稳定编码工具链

推荐执行顺序为：

```text
search_text → read_file → edit_file/write_file → bash验证 → 复查差异
```

- `search_text`：按路径、文件名 glob 和结果上限返回 `path:line`，避免把整个仓库塞入上下文；
- `edit_file`：要求旧文本精确匹配指定次数，并检查 `read_file` 记录的文件版本；使用临时文件、`fsync` 和 `os.replace` 原子提交；
- `write_file`：同样使用原子替换，失败时旧文件保持不变；
- `bash`：超时或协程取消时终止整个子进程组，避免 Agent 已结束但测试进程仍残留。

## Artifact 专用读取

大型工具结果外置在 Run 目录，不属于项目 Workspace。普通 `read_file` 不应为取回 Artifact 而放宽边界，因此提供 `read_artifact`：它只能访问当前 Run 的 Artifact 目录，并通过 `offset + max_chars` 分页读取。这样完整信息可恢复，又不会一次读回后再次撑爆上下文。

## 验证

单元测试覆盖绝对路径、`..`、符号链接逃逸、精确编辑冲突、外部修改冲突、原子替换失败回滚、检索预算、Plan Mode 安全契约和 Artifact 分页读取。`evals.system_eval` 的 `governance` 套件将其中四类机制固化为可重复执行的系统能力评测。
