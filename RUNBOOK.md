# 运维手册（RUNBOOK）

## 日常操作

### 启动守护进程

```bash
uv run kama core start          # 官方封装：后台启动并记录 PID 文件
# 或等价地：
uv run kama-core
```

默认监听 `127.0.0.1:7437`，按 `Ctrl+C` 优雅退出。

配置文件优先级（低 → 高）：**内建默认值 → `--config` 显式路径 → `KAMA_CONFIG` 环境变量 → `~/.kama/config.toml`**。
显式指定但文件不存在时**直接报错退出**，不再静默回退默认值（避免误用错配置）。
推荐把日常配置放到 `~/.kama/config.toml`（见 [docs/model-switching.md](./docs/model-switching.md)）。

```bash
uv run kama-core --config /abs/path/to.toml     # 临时换配置
uv run kama core start -c /abs/path/to.toml      # 后台启动 + 临时换配置
```

启动日志会打印实际配置来源与可用模型：

```
config source: /home/user/.kama/config.toml
models: default=anthropic available=anthropic, ollama, auto  (use /model to switch)
```

### 验证连通

```bash
uv run kama ping
# → pong server=0.0.1 uptime=12ms latency=2ms
```

### 停止守护进程

```bash
kill $(pgrep -f kama-core)
```

---

## 运行时切换模型（/model）

只要配置里含多个 `[[llm.providers]]`（daemon 启动日志会显示 `models: ...`），即可在
`kama chat` / TUI 会话里运行时切换，无需重启：

| 命令 | 效果 |
|------|------|
| `/model` | 列出当前模型与全部可选项，标出当前项（`*`） |
| `/model ollama` | 切到 ollama（本地）；`/model anthropic` 切回 API |
| `/model auto` | 恢复配置默认 provider 并重新启用 fallback |
| `/model list` / `/model ls` | 等同 `/model` |

- 切换是内存态，**不写文件**，重启后回到 `default_provider`。
- 显式 `/model <name>` 后进入锁定态：该 provider 失败直接报错，不再静默回退；
  `/model auto` 解除锁定。
- 单 provider 配置下 `/model` 会提示 `model switching unavailable`。

详细设计与延迟初始化（lazy init）说明见 [docs/model-switching.md](./docs/model-switching.md)。

---

## 配置

优先级（低 → 高）：**内建默认值 → `~/.kama/config.toml` → `KAMA_CONFIG` 环境变量 → `--config` 显式路径**。

### `~/.kama/config.toml`

```toml
[core]
host = "127.0.0.1"
port = 7437

[logging]
level  = "INFO"
file   = "~/.kama/logs/core.log"
format = "text"    # "text" | "json"
```

### `.env`

从 `.env.example` 复制后修改，存放本机配置与密钥（不提交 git）：

```bash
cp .env.example .env
```

### 系统环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KAMA_CONFIG` | `~/.kama/config.toml` | 覆盖配置文件路径（低于 `--config` 显式路径；文件不存在直接报错） |
| `KAMA_HOST` | `127.0.0.1` | TCP 监听地址 |
| `KAMA_PORT` | `7437` | TCP 监听端口 |
| `KAMA_LOG_LEVEL` | `INFO` | 日志级别（DEBUG / INFO / WARNING / ERROR） |
| `KAMA_LOG_FILE` | `~/.kama/logs/core.log` | 日志文件路径（留空则仅输出 stderr） |
| `KAMA_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |

---

## 开发

```bash
uv run ruff check src tests scripts   # lint
uv run mypy src                       # 类型检查
uv run pytest tests/ -v               # 全量测试
uv run pytest tests/unit/ -v         # 仅单元测试（无需启动 daemon）

make docs                             # 重新生成 WIRE_PROTOCOL.md
make verify-s0                        # 完整验证（lint + 类型 + 测试 + 协议同源检查）
```

---

## 日志

```bash
tail -f ~/.kama/logs/core.log
```

---

## 常见错误

| 报错 | 原因 | 处理 |
|------|------|------|
| `core already running at 127.0.0.1:7437` | 已有守护进程在运行 | `kill $(pgrep -f kama-core)` |
| `core not running` | 未启动守护进程 | `uv run kama-core` |
| `Address already in use` | 端口被其他进程占用 | `KAMA_PORT=8000 uv run kama-core` |
| `Config error: KAMA_PORT must be an integer` | `.env` 或环境变量中端口值非整数 | 检查 `KAMA_PORT` 的值 |
