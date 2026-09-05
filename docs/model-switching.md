# 模型配置与运行时切换（`/model`）

本文说明两件事：

1. 怎么**简化启动**——不用每次 `cd` 到项目目录、不用每次敲 `KAMA_CONFIG=... kama-core`。
2. 怎么**在 anthropic（API Key）与 ollama（本地）之间运行时切换**——进会话后敲 `/model`。

## 1. 一句话结论

- **最省事**：把一份同时含 anthropic + ollama 的配置放到默认位置 `~/.kama/config.toml`，
  之后永远只敲 `kama core start`，再也不用 `KAMA_CONFIG=...` 也不用进项目目录。
- **会话里切换**：`/model ollama` 切到本地模型，`/model anthropic` 切回 API，`/model auto`
  恢复配置默认（并重新启用 fallback），`/model` 不带参数查看当前与全部可选项。
- **临时换配置**才用 `--config`：`kama-core --config /abs/path/to.toml` 或
  `kama core start -c /abs/path/to.toml`。

## 2. 三种启动方式对比

| 方式 | 命令 | 何时用 |
|------|------|--------|
| 默认配置 | `kama core start` | 已把配置放到 `~/.kama/config.toml`（推荐） |
| 显式路径 | `kama-core --config /abs/path.toml` | 偶尔换一份配置、且不想改默认文件 |
| 环境变量 | `KAMA_CONFIG=/abs/path.toml kama-core` | 兼容老写法；**路径不存在会直接报错** |

> 默认查找顺序：`--config` 显式路径 > `KAMA_CONFIG` 环境变量 > `~/.kama/config.toml`。
> 显式指定但文件不存在时**直接 `SystemExit`**，不再静默回退默认值——
> 避免"以为在用 ollama、其实在查 ANTHROPIC_API_KEY"这类坑。

## 3. 推荐配置：一份文件两种模型

复制示例到默认位置即可（一次操作，永久生效）：

```bash
cp examples/multi.toml ~/.kama/config.toml
kama core start
kama chat
```

`examples/multi.toml` 内容（anthropic 默认 + ollama fallback）：

```toml
[llm]
router = "static"
default_provider = "anthropic"          # 想默认走本地就改成 "ollama"
fallback_providers = ["ollama"]

[[llm.providers]]
name = "anthropic"
kind = "anthropic"
model = "claude-sonnet-4-6"
api_key_env = "ANTHROPIC_API_KEY"

[[llm.providers]]
name = "ollama"
kind = "openai_compatible"
model = "qwen3.8:27b"
base_url = "http://192.168.1.130:11434/v1"
api_key_env = ""                        # 本地模型留空，跳过 key 校验
context_window = 262144
```

只要配置里出现**多于一个** `[[llm.providers]]`，`build_provider` 就会返回
`ProviderRouter`，`/model` 才会可用；否则会提示
`model switching unavailable: no provider router configured`。

## 4. /model 用法

进入 `kama chat` 或 TUI 后在输入框键入：

| 输入 | 效果 |
|------|------|
| `/model` | 列出当前模型、全部可选项、`auto`，并标出当前项（`*`） |
| `/model ollama` | 切换到 ollama（`/model anthropic` 同理） |
| `/model auto` | 恢复配置默认并重新启用 fallback |
| `/model list` / `/model ls` | 等同 `/model` |

`/model` 是**本地控制命令**：不会触发任何 agent run，只在当前会话回复一条系统提示
（`session.notice` 事件），CLI 用普通文本打印、TUI 用黄色 `model` 行展示。

切换通过 `ProviderRouter.switch_model` 修改内存里的默认 provider，**不写任何文件**，
进程重启后回到 `default_provider`。

## 5. 关键设计：客户端延迟初始化（lazy init）

历史版本里 `AnthropicProvider` / `OpenAICompatibleProvider` 在**构造时**就校验 API Key，
缺 Key 直接 `SystemExit`。这意味着"一份配置同时含 anthropic + ollama"会因为它俩一起被
实例化而导致 daemon 启动即崩——即使你只打算用 ollama。

现在改为**延迟初始化**：

- 构造时只记录 `model / api_key_env / base_url`，**不创建客户端、不校验 Key**。
- 真正的客户端在第一次 `chat()` 时才按需创建（`_ensure_client()`）。
- 因此：本机没设 `ANTHROPIC_API_KEY` 也能正常启动，只要当前走的是 ollama；
  只有真正 `/model anthropic` 时才因为没有 Key 报错。反之亦然（ollama 没开时启动不挂，
  切过去才报错）。

> 单元测试已随之更新：`test_missing_api_key_defers_until_first_chat`、`test_openai_compatible_allows_unauthenticated_endpoint`、
> `test_openai_compatible_still_requires_configured_key` 现在断言"构造不报错、首次 chat 才失败"。

## 6. 切换语义：显式锁定 vs 自动 fallback

- **未显式 `/model`**（`auto` 态）：主 provider 连续失败达到阈值（默认 2 次）后，
  自动回退到 `fallback_providers`（如上面的 ollama）。这是"保底不停机"行为。
- **已显式 `/model ollama`**：进入"锁定"态，只走 ollama；若该 provider 失败**直接报错**，
  不再偷偷退回 anthropic。理由：用户明确点名要本地模型，静默回退会违背意图、还可能烧掉
  API 额度。
- `/model auto` 解除锁定，恢复配置默认并重新启用 fallback。

## 7. 配置来源可见性

启动时日志会打印实际加载的配置文件与可用模型，方便确认"现在到底用的是哪份配置"：

```
config source: /home/user/.kama/config.toml
models: default=anthropic available=anthropic, ollama, auto  (use /model to switch)
```

`kama core status` 也会回显当前 daemon 是用哪份配置启动的。

## 8. 相关代码位置

| 文件 | 职责 |
|------|------|
| `src/kama_claude/core/llm/router.py` | `ProviderRouter`：模型列表、`switch_model`、`current_model`、锁定态与 fallback |
| `src/kama_claude/core/llm/factory.py` | 多个 `[[llm.providers]]` → 返回 `ProviderRouter` |
| `src/kama_claude/core/llm/provider.py` | `AnthropicProvider` 延迟初始化 |
| `src/kama_claude/core/llm/openai_provider.py` | `OpenAICompatibleProvider` 延迟初始化 |
| `src/kama_claude/core/config.py` | `get_config` 支持显式路径、`--config`、缺失即报错 |
| `src/kama_claude/core/app.py` | `kama-core` 解析 `--config`，启动日志打印来源与模型 |
| `src/kama_claude/cli/main.py` | `kama -c/--config` 与 `kama core start -c/--config` |
| `src/kama_claude/cli/commands/core.py` | `cmd_core_start` 把 `--config` 透传给 daemon 子进程 |
| `src/kama_claude/core/session/manager.py` | `/model` 命令解析与 `session.notice` 广播 |
| `src/kama_claude/core/bus/events.py` | `SessionNoticeEvent` 定义 |
| `src/kama_claude/cli/commands/chat.py` / `src/kama_claude/tui/app.py` | 客户端渲染 `session.notice` |

## 9. 测试

```bash
uv run pytest tests/unit/test_model_switch.py -v   # /model 命令 + router 切换
uv run pytest tests/unit/test_provider_router.py -v
uv run pytest tests/unit/test_llm_provider.py tests/unit/test_openai_provider.py -v  # 延迟初始化
```
