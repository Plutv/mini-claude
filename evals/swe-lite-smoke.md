# SWE-bench Lite 小批量真实任务验证

数据来源：[SWE-bench Lite，dev 划分](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite)。
该划分有 23 道真实 GitHub issue。本轮选择三道来自不同仓库、依赖较轻的任务，
用于观察 KC 当前代码在新任务上的表现。选择不是随机抽样，不能据此宣称正式 SWE-bench 成绩。

| Issue | 问题 | Agent 工作区 |
| --- | --- | --- |
| pydicom__pydicom-1694 | JSON 转换未正确抑制无效字段异常 | [Pydicom](/e:/project_2026/python/KamaClaude/workspace/swe-lite-smoke-20261001/pydicom__pydicom-1694) |
| pylint-dev__astroid-1268 | Unknown 节点缺少字符串输出方法 | [Astroid](/e:/project_2026/python/KamaClaude/workspace/swe-lite-smoke-20261001/pylint-dev__astroid-1268) |
| sqlfluff__sqlfluff-2419 | L060 提示没有指出实际违规函数 | [SQLFluff（重跑）](/e:/project_2026/python/KamaClaude/workspace/swe-lite-smoke-20261001-retry/sqlfluff__sqlfluff-2419) |

## 验证过程

1. 检出数据集指定的 base commit，准备独立 Python 3.10 测试环境。
2. 从 base commit 建立验证副本，应用官方 test_patch，确认 FAIL_TO_PASS 用例失败。
3. 在另一个副本应用官方修复及 test_patch，确认目标用例和相关测试文件通过。
4. 只有环境检查通过后，才将原始 issue 描述交给 KC。Agent 工作区没有官方新增测试和官方修复。
5. KC 完成后导出它的补丁，在全新的验证副本应用 Agent 补丁；将官方 test_patch 涉及的测试文件恢复到 base commit，再应用官方测试补丁并运行。

恢复测试文件只发生在派生验证副本，不修改 Agent 工作区；避免候选补丁改变测试或导致官方补丁冲突。
这一处理与 [SWE-bench 官方验证器对测试文件的恢复方式](https://github.com/swe-bench/SWE-bench/issues/518)一致。
另行执行 `audit`，检查 Agent 实际交付的公开测试和新增测试；官方回归过关不等于交付质量过关。

评分同时要求非空补丁、目标回归通过、相关测试文件通过。`run.status` 记录执行终止状态，
`resolved` 记录独立验证结果，二者分别保存。测试收集错误、依赖安装错误归为环境问题。
相关测试文件检查并不等同于运行数据集完整 PASS_TO_PASS 列表。

本轮调用 KC 的生产 AgentLoop、Registry、文件版本检查、权限调用链和 Context Engine。
保留读取、搜索、修改、Artifact 和 git_diff 工具；run_tests 在评测中替换为固定的公开测试命令。
不加载个人长期记忆与 Skills，也没有任意 Bash 命令、子 Agent 或客户端通信。
因此这轮主要验证代码定位和修改能力，不覆盖完整 CLI/TUI 产品链路。

## 文件说明

执行入口：[swe_lite_pilot.py](/e:/project_2026/python/KamaClaude/evals/swe_lite_pilot.py)。
结果目录：[swe_lite_smoke_20261001](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001)。
每道 issue 下包含：

- `task.md`：实际交给 Agent 的原始 issue 描述。
- `case.json`、`official_test.patch`：原始数据和测试补丁，只供验证器读取。
- `candidate.patch`：Agent 最终修改。
- `agent/events.jsonl`、`agent/messages.json`：工具调用、流输出、完整对话。
- `agent/run.json`：步数、工具失败、Token 使用和终止原因。
- `baseline/`、`gold/`、`candidate/`：各阶段的目标测试和相关回归日志、JUnit XML。
- `report.json`：环境有效性和最终验证结果。
- `python_environment.json`：本次实际使用的测试解释器和包版本。
- `delivery.json`：原始交付检查，包含 Agent 新增的测试。

工作区放在 Git 已忽略的 `workspace/`，不提交第三方完整仓库。
验证副本默认放在 WSL 文件系统的测试环境旁，避免 Windows 挂载盘解包大量小文件的开销；也可通过 `--verifiers` 指定。
case.json 包含官方修复，不能作为 Agent 输入，也不能用于训练或调整本轮答案。

## 重跑

在 WSL 中进入 KC 根目录，使用新的输出目录和工作区，避免覆盖已有实验：

```bash
uv run --no-project --with pyarrow python -m evals.swe_lite_pilot prepare \
  --dataset "$DATASET_PARQUET" \
  --output evals/results/swe_lite_retry \
  --workspaces workspace/swe-lite-retry \
  --environments "$TEST_ENV_ROOT"

python -m evals.swe_lite_pilot run \
  --case pydicom__pydicom-1694 \
  --output evals/results/swe_lite_retry \
  --workspaces workspace/swe-lite-retry \
  --environments "$TEST_ENV_ROOT" \
  --max-steps 20 --timeout 600
```

DATASET_PARQUET 指向下载的 dev.parquet，TEST_ENV_ROOT 指向 WSL 文件系统中的独立测试环境目录。
KC 的 LLM 配置从项目现有配置读取；禁止自动切换备用供应商。API Key 不写入报告。
每道任务先跑一次用于排查功能和环境；重复实验或严格 A/B 需要保持模型、权限、Prompt 和步数预算一致。

## 本轮结果与限制

完整结果及对应日志见 [本轮报告](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/summary.md)。

SQLFluff 初次运行由操作人员误判进度后中断，保留记录但不计失败；重跑从干净工作区开始。
本轮运行时的 SQLFluff 公开测试配置误用了 `std_test.py -k L060`，实际未选中测试。
事后在原候选工作区用 `yaml_test_cases_test.py -k L060` 补验了 3 条测试，均通过。
该补验没有重新调用模型，不应理解为 Agent 当时已经完成验证。
后续脚本已纠正测试选择，并在模型调用前检查公开测试是否确实收集到可执行用例。
