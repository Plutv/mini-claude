# 结果表数据契约

| Table | Purpose | Rows | Metrics | Data source | Replacement owner |
|---|---|---|---|---|---|
| main-resolution | 主基线比较 | B0/B1/B2 | resolved rate、hidden pass、mean steps、mean tokens、mean duration | 每次Run原始JSONL与测试日志 | eval runner |
| context-ablation | Context Engine消融 | A1-A5 | resolved rate、input tokens、compactions、artifact chars | 事件与Artifact元数据 | eval runner |
| robustness | 故障恢复 | fault kind × injection point | recovery rate、duplicate side effects、balanced history | fault injection logs | eval runner |
| category-breakdown | 分类别能力 | config/persistence/async/security/retry/rpc | macro pass、mean attempts | case metadata + verification | aggregator |

重复运行的数值字段报告`mean ± std`并保留样本数；成功率同时给出Wilson 95%置信区间。
