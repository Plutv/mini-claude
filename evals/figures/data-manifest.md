# 图表数据清单

当前尚无真实Agent运行结果，因此不生成结果图。

后续数据文件约定：

| File | Producer | Consumers | Status |
|---|---|---|---|
| `results/runs.jsonl` | eval runner | 主结果与消融聚合 | 待生成 |
| `results/case_summary.csv` | aggregator | 类别成功率图 | 待生成 |
| `results/context_ablation.csv` | aggregator | Token—成功率权衡图 | 待生成 |

禁止用模拟数值替代真实Agent运行结果；规划数据必须使用`mock_`或`synthetic_`前缀。
