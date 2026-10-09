# 三道真实 issue 的小批量验证

整理日期：2026-10-02。使用 SWE-bench Lite 的 dev 划分（23 道），本轮选择 3 道不同仓库的 issue，
每道保留一份完整模型尝试。它不是 300 道 test 划分，也不是正式 Docker 评测成绩。

## 结果

| Issue | 官方目标测试 | 官方目标所在文件回归 | 实际交付检查 |
| --- | --- | --- | --- |
| Pydicom #1694 | 1 通过 | 27 通过 | Agent 新增测试有错误，公开测试 26 通过、1 失败 |
| Astroid #1268 | 1 失败 | 95 通过、1 失败、1 跳过 | 未修改实现，只留下失败的调试测试 |
| SQLFluff #2419 | 1 通过 | 1 通过（同一测试，不能重复计数） | 事后更正公开测试选择后，L060 的 3 条测试通过 |

**两道源码补丁通过了本轮官方回归检查，但不能说两道都完整交付成功。**
三次 Run 都达到了 20 Step；模型最终回复仍是原始 DSML 工具调用文本，运行时却记为 `success`。
因此运行时结束状态、问题修复结果和交付质量必须分开看。

| Issue | Step | 工具调用数 | Agent 阶段耗时 |
| --- | ---: | ---: | ---: |
| Pydicom | 20 | 25 | 58.52 秒 |
| Astroid | 20 | 33 | 85.47 秒 |
| SQLFluff（重跑） | 20 | 29 | 129.33 秒 |

耗时不含安装依赖和独立验证，工具调用数不等于底层进程执行次数（失败重试会增加执行次数）。

## 看实际产物

- Pydicom：[原始 issue](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pydicom__pydicom-1694/task.md)、[候选补丁](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pydicom__pydicom-1694/candidate.patch)、[官方验证报告](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pydicom__pydicom-1694/report.json)、[新增测试失败原因](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pydicom__pydicom-1694/delivery/pytest.log)。
- Astroid：[原始 issue](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pylint-dev__astroid-1268/task.md)、[候选补丁](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pylint-dev__astroid-1268/candidate.patch)、[官方测试失败原因](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pylint-dev__astroid-1268/candidate/target/pytest.log)、[调试测试失败](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/pylint-dev__astroid-1268/delivery_new_tests/pytest.log)。
- SQLFluff：[原始 issue](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/retry/sqlfluff__sqlfluff-2419/task.md)、[候选补丁](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/retry/sqlfluff__sqlfluff-2419/candidate.patch)、[官方验证报告](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/retry/sqlfluff__sqlfluff-2419/report.json)、[事后公开测试补验](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/retry/sqlfluff__sqlfluff-2419/delivery_corrected/pytest.log)。

## 本轮暴露的问题

1. **测试失败被当成可重试运行错误。** 相同 pytest 失败被原样重跑，既不修代码，也不产生新反馈。
2. **到达步数边界后，收尾没有正常完成。** 虽然最后一轮不提供工具，模型仍输出 DSML 文本；`end_turn` 被当成运行成功，不能证明任务完成。
3. **缺少清理调试产物和检查新增测试的交付门槛。** Astroid 留下 `assert 0` 的探针；Pydicom 的测试输入选择错误。
4. **评测工具也有缺陷。** SQLFluff 原公开命令没有选中任何测试，固定测试工具也不允许模型运行新建探针。这会影响结果，不能全部归因于模型或 KC。

本轮只修正了验证流程：官方测试文件恢复、交付检查、SQLFluff 的后续测试选择，以及下一轮模型调用前的公开测试预检。
没有在本轮中途修改 KC 核心来追求更好成绩，也没有人工修补候选答案。

## 有效性与边界

- 三道均验证了 base commit 上官方目标失败、官方修复后通过，避免把依赖问题当成 Agent 能力问题。
- 官方测试文件在派生副本中恢复后再应用 test_patch。Agent 自己的测试另行检查，原工作区和候选补丁完整保留。
- SQLFluff 原尝试被操作人员误判进度后中断，[中断记录](/e:/project_2026/python/KamaClaude/evals/results/swe_lite_smoke_20261001/sqlfluff__sqlfluff-2419/interrupted.json)保留但不计失败。重跑使用全新工作区。
- SQLFluff 补验没有模型参与，也不是 Agent 当时收到的反馈；原先“零测试选中”的日志没有被覆盖。
- 运行配置请求模型名为 `claude-sonnet-4-6`，实际配置的端点是 `api.deepseek.com`。不能因此声称使用了 Claude；服务端实际映射模型身份未验证。Ollama 没有参与回退。
- 只运行官方目标和其所在测试文件，未覆盖完整 PASS_TO_PASS 或项目全量测试。样本是便利选择，每题单次，不做统计显著性和泛化成功率推断。
- 未覆盖完整 RPC、会话恢复、Skills、长期记忆或子 Agent 链路；详见[执行协议](/e:/project_2026/python/KamaClaude/evals/swe-lite-smoke.md)。

下一轮应先修复测试反馈与收尾机制，再用相同预算重跑开发集；新的评估结论必须与本轮记录分开保存。
