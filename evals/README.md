# KC Coding Agent Evaluation

这套评测用于回答一个具体问题：KamaClaude 能否在隔离代码仓库中根据任务描述完成修改，并让公开测试与隐藏测试同时通过。

## 数据分层

1. `local_v1`：仓库内自建、完全离线、可重复的小型工程任务。隐藏测试不会复制到 Agent 工作区。
2. QuixBugs：40 个 Python/Java 单行算法缺陷，适合验证基础修复能力。
3. Defects4J / GitBug-Java：真实 Java 项目缺陷，适合验证跨文件定位、构建与测试能力。
4. SWE-bench Verified：真实 GitHub Issue，环境与成本较高，只用于最终外部效度验证。

## 本地数据集用法

列出任务：

```bash
python -m evals.harness list
```

准备一个不包含隐藏测试的工作区：

```bash
python -m evals.harness prepare config-precedence --output workspace/config-precedence
```

将 `workspace/config-precedence/TASK.md` 作为 Agent 目标。Agent 完成修改后验证：

```bash
python -m evals.harness verify config-precedence --workspace workspace/config-precedence
```

验证数据集自身满足“初始版本失败、标准答案通过”：

```bash
python -m evals.harness validate
```

## Runtime系统能力评测

运行不依赖真实模型的确定性评测：

```bash
python -m evals.system_eval --output evals/results/system_eval_latest.json
```

也可以单独运行一个能力域：

```bash
python -m evals.system_eval --suite context
python -m evals.system_eval --suite memory
python -m evals.system_eval --suite recovery
```

当前包含：

- `context`：大结果和大错误的Artifact完整性、上下文载荷缩减、当前请求保护、工具消息配对；
- `memory`：小型标注集的`Hit@1/Hit@3`、Scope隔离、内容去重和召回字符预算；
- `recovery`：原子快照、非配对历史拒绝、替换失败回滚、损坏JSONL尾部和孤立`tool_use`恢复。

报告中的`limitations`是结论边界：这套评测验证Runtime机制，不等价于真实模型的代码任务成功率，也不能用于宣称SWE-bench成绩。

## 防止结果失真

- Agent 只能看到 `seed/` 复制出的工作区和 `TASK.md`，不能把 `oracle/` 或 `solution/` 加入工作区。
- 同一模型、Prompt、工具权限、最大步骤和上下文策略至少重复运行 3 次。
- 原始事件、最终补丁、测试输出和Token用量必须保留，不得只记录成功率。
- `local_v1` 是开发集；调参后必须用未参与调试的开源任务验证泛化能力。
- 合成任务结果不能宣称为 SWE-bench 或真实生产任务成绩。
