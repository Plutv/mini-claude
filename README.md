# KamaClaude

本地 AI Agent 系统。`kama-core` 作为常驻守护进程处理所有任务，`kama`（CLI）和 `kama-tui`（TUI）通过 TCP loopback 与之通信。

核心能力包括：Session 级 Workspace 绑定、ReAct 工具循环、结构化代码检索与原子编辑、分层上下文治理、长期记忆、Plan Mode、父子 Agent 编排、MCP 工具接入以及确定性故障评测。

## 环境要求

| 依赖 | 版本 |
|------|------|
| 操作系统 | macOS / Linux |
| Python | 3.12.x |
| [uv](https://docs.astral.sh/uv/) | ≥ 0.4 |

安装 uv（若尚未安装）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Python 3.12 由 uv 自动管理，无需手动安装。

## 快速开始

```bash
git clone <repo> && cd KamaClaude
uv sync
cp .env.example .env        # 按需修改

uv run kama-core &          # 启动守护进程（后台）
uv run kama ping            # 验证连通：应返回 pong
uv run kama --version       # 应输出 0.0.1
```

客户端从哪个目录创建 Session，该目录就会成为持久化 Workspace。恢复 Session 后仍沿用原 Workspace，文件工具和子 Agent 不会退回到 `kama-core` 的启动目录。

## 质量验证

```bash
uv run ruff check src tests evals
uv run mypy src
uv run pytest -q
uv run python -m evals.system_eval
uv run python -m evals.context_quality_eval
# 调用当前配置的真实模型做 3 轮完整上下文 / 治理后 A/B
uv run python -m evals.context_quality_eval --live --repetitions 3
```

## 文档

- **[RUNBOOK.md](./RUNBOOK.md)** — 完整操作参考：配置、开发命令、故障排查
- **[WIRE_PROTOCOL.md](./WIRE_PROTOCOL.md)** — IPC 协议定义（由代码生成，勿手动编辑）
- **[docs/workspace-and-tools.md](./docs/workspace-and-tools.md)** — Workspace 隔离与结构化工具设计
- **[evals/context-quality.md](./evals/context-quality.md)** — 上下文回答质量 A/B 设计与实跑结果
