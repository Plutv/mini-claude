# 方法—实验可追溯矩阵

| Contribution | Method module | Experiment | Table/Figure | Allowed claim | Evidence status |
|---|---|---|---|---|---|
| 分层上下文治理 | ContextEngine | B0 vs B1 + A1-A5 | main-context | 在固定任务与模型下比较成功率和Token | 协议已定义，待真实运行 |
| Artifact外置 | ToolArtifactStore | 10万字符压力任务 + A1 | artifact-efficiency | 上下文字符载荷缩减率 | 已有合成基准，缺真实任务 |
| 配对校验与原子快照 | SessionStore/AgentLoop | 取消与写入故障注入 | recovery | 异常下保留上一完整消息快照 | 单元测试已有，缺进程级故障实验 |
| 父子Agent审查 | SpawnAgentTool | B1 vs A6 | reviewer-ablation | Reviewer对隐藏测试通过率的影响 | 待运行 |
| Coding Agent落地 | Eval harness + local/open-source cases | 全部主实验 | main-resolution | 在指定数据集上的任务解决率 | 本地数据集建设中 |
