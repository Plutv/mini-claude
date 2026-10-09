# Mini Claude · 本地智能编码助手

面向本地代码仓库的终端 Agent：读取和检索代码、执行多步骤修改、运行测试，并记录执行过程。
支持 CLI 和 TUI 两种入口，执行核心通过 JSON-RPC 接收请求，通过事件流推送模型输出、工具结果和权限请求。

## 功能

- **代码操作**：文件读取、代码搜索、补丁修改、测试执行及 Git diff 查看；文件修改包含工作区路径检查和版本冲突检查。
- **执行控制**：ReAct 工具循环、权限审批、Plan Mode 和父子 Agent 任务管理。
- **上下文与记忆**：长工具结果外置、陈旧结果清理、局部压缩及摘要；SQLite 保存 Session、Project、Global 作用域的长期记忆。
- **会话管理**：为 Session 绑定 Workspace，保存对话和运行记录，支持恢复已保存的会话。
- **扩展接入**：Skills 指令扩展、MCP 工具接入，以及 Anthropic / OpenAI 兼容模型接口。

项目名称为 Mini Claude；安装包、命令和配置仍沿用 `kama` 命名，便于兼容现有环境。

## 环境要求

Python 3.12、[uv](https://docs.astral.sh/uv/)，运行环境为 Linux 或 macOS。Windows 可通过 WSL 使用。

## 快速开始

```bash
git clone https://github.com/Plutv/mini-claude.git
cd mini-claude
uv sync
cp .env.example .env
```

编辑 `.env`，设置 `ANTHROPIC_API_KEY` 和需要使用的模型；接入兼容服务时，还需确认模型名称与 API 协议匹配。
不要提交实际密钥。

终端 A 启动执行核心：

```bash
uv run kama-core
```

终端 B 测试连接，并启动交互会话：

```bash
uv run kama ping
uv run kama chat
# 或启动终端界面
uv run kama-tui
```

也可以提交单次任务：

```bash
uv run kama run --goal "阅读 README.md，说明项目入口和目录结构，不修改文件"
```

交互客户端创建 Session 时绑定当前目录为 Workspace；恢复会话后继续使用原 Workspace，而不是 Core 的启动目录。

### 后台运行与会话恢复

```bash
uv run kama core start
uv run kama core status
uv run kama sessions
uv run kama chat --last
uv run kama core stop
```

`kama-core` 是前台运行方式；`kama core start` 才会创建脱离启动终端的后台进程。
恢复会话是重新加载保存的历史，并不表示服务崩溃后会自动从中断步骤继续执行。

### 使用 Ollama

修改 [Ollama 示例配置](examples/ollama.toml)，将服务地址和模型名改成自己的设置，然后运行：

```bash
uv run kama-core --config examples/ollama.toml
```

本地 Ollama 不需要 API Key。多个 Provider 的配置和运行时切换见[模型配置说明](docs/model-switching.md)。

## 开发与验证

```bash
uv run ruff check src tests evals
uv run pytest tests/unit -q
uv run python -m evals.system_eval
uv run python -m evals.context_quality_eval
```

真实模型评测会调用配置的模型，可能产生费用：

```bash
uv run python -m evals.context_quality_eval --live --repetitions 3
```

单元测试、合成上下文评测和真实代码任务评测验证的对象不同，不将工具输出缩减率等同于任务成功率。

## 使用边界

- 权限审批和文件路径检查不是操作系统沙箱；Shell 命令仍在宿主机运行，建议先在独立工作区使用。
- 客户端断开与执行核心退出是两回事：前者不必结束任务，后者会影响正在执行的任务。
- 模型输出和运行状态不能替代验证；修改代码后应检查 diff 并运行相应测试。

## 文档

- [操作手册](RUNBOOK.md)
- [通信协议](WIRE_PROTOCOL.md)
- [工作区与工具设计](docs/workspace-and-tools.md)
- [评测说明](evals/README.md)

## 许可

采用 [MIT License](LICENSE)。本仓库基于 KamaClaude 迭代，保留原始版权声明；扩展代码遵循同一许可。
