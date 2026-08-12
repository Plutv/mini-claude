# KC Coding Agent 评测协议

## 研究问题

1. KC能否完成具有确定测试判据的代码修改任务？
2. Context Engine v2能否降低上下文与Token开销，同时不降低任务完成率？
3. 权限、重试、子Agent等Runtime机制是否改善可靠性，或引入额外失败？

## 数据集与划分

- 开发集：`local_v1`前4个任务，用于打通执行链路，不作为最终泛化结论。
- 本地留出集：`local_v1`后2个任务，在策略冻结后运行。
- 外部轻量集：QuixBugs Python/Java各10个任务。
- 外部真实集：Defects4J/GitBug-Java 5个任务；最终再抽取5到10个SWE-bench Verified任务。

任务按`case_id`固定划分，不允许根据单次失败把任务移出数据集。

## 公平基线

- B0：当前KC Runtime，关闭Context Engine v2的分层治理。
- B1：KC + Context Engine v2。
- B2：同一模型的LangGraph ReAct基线，提供相同工具和最大步骤。
- B3（可选）：同一模型直接生成一次补丁，不允许迭代工具调用。

所有基线固定模型、温度、最大输出Token、任务Prompt、工具权限和执行超时。

## 主要指标

- `resolved_rate`：隐藏测试全部通过的任务比例，主指标。
- `public_pass_rate`与`hidden_pass_rate`：区分表面修复与泛化修复。
- `regression_free_rate`：原本通过的测试仍然通过。
- `valid_patch_rate`：产生可应用且不为空的代码修改。
- `steps`、`tool_calls`、`tool_failures`、`duration_ms`。
- `input_tokens`、`output_tokens`、`cache_read_tokens`及估算费用。
- `context_compactions`和Artifact外置字符数。

每个配置至少重复3次，报告均值、标准差和95%置信区间。任务不平衡时同时报告宏平均和按类别分组结果。

## 主实验

在同一任务集和模型下比较B0、B1、B2，主要观察`resolved_rate`、总输入Token和耗时。只有当成功率未下降且开销下降时，才允许声称Context Engine改善长任务效率。

## 消融实验

- A1：关闭Artifact外置。
- A2：关闭陈旧工具结果清理。
- A3：关闭Microcompact。
- A4：关闭LLM全量摘要。
- A5：关闭Prompt Cache热前缀保护。
- A6：关闭子Agent Reviewer。

## 鲁棒性实验

- 在第N次LLM调用注入连接失败或限流。
- 在工具调用注入超时和RuntimeError。
- 在完整步骤落盘前后取消任务，验证快照配对完整性。
- 对超大工具输出、Unicode路径、恶意路径穿越任务单独统计。

## 运行环境与日志契约

每次运行记录：Git commit、数据集版本、case_id、模型、Provider、随机种子、最大步骤、上下文策略、开始/结束时间、事件文件、最终补丁、公开/隐藏测试输出和失败分类。

任何模拟数据文件必须以`mock_`或`synthetic_`开头，且不得写入最终结果结论。
