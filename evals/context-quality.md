# Context Engine 回答质量 A/B

单独报告“10 万字符工具结果缩减 95.7%”没有回答最重要的问题：信息被裁剪后，模型还能否完成后续问答。因此该数字只保留为压力场景的资源指标，不能作为效果结论。

## 数据集

`datasets/context_quality_v1.json` 包含 6 类合成问答：

1. 关键信息位于大工具结果头部；
2. 关键信息位于大工具结果中部；
3. 关键信息位于大工具结果尾部；
4. 根因信息位于大错误输出中部；
5. 同一文件被重复读取，以最新结果为准；
6. 大量历史噪声后仍需正确处理当前请求。

中部样本经过 Artifact 外置后，答案不会出现在头尾预览里。模型必须根据引用调用 `read_artifact(query="RECOVERY_CODE")`，从完整工件中定位并补取；这使评测能够区分“信息仍可恢复”和“模型实际会恢复”。

## 两层验证

离线检查不调用模型，只验证关键信息仍在活跃消息或 Artifact 中、`tool_use/tool_result` 配对完整，而且中部样本确实已从活跃消息移除：

```bash
python -m evals.context_quality_eval \
  --output evals/results/context_quality_offline_latest.json
```

真实模型 A/B 在相同问题上比较完整上下文基线和 Context Engine 实验组：

```bash
python -m evals.context_quality_eval --live --repetitions 3 \
  --output evals/results/context_quality_live_latest.json
```

## 当前结果

使用 `deepseek-v4-flash`，6 类问题各重复 3 次，共 18 组观测：

| 指标 | 完整上下文 | Context Engine | 变化 |
|---|---:|---:|---:|
| Exact Match | 100% | 100% | 0 个百分点 |
| 累计 Prompt Token | 562,599 | 146,022 | -74.05% |
| 首轮活跃字符 | 基线 | 实验组 | -90.41% |

累计 Prompt Token 包含 Prompt Cache 读取量，也包含模型调用 `read_artifact` 后产生的额外请求；因此它反映端到端上下文开销，不是只统计裁剪后的第一轮请求。实验组共发生 10 次 Artifact 工具调用，这部分额外轮次也是治理策略的真实代价。

## 结论边界

这套结果支持的结论是：在当前合成上下文保真任务中，治理后准确率从 100% 维持在 100%，累计 Prompt Token 下降 74.05%。它不能证明真实代码修复成功率提升，也不能替代 local_v1、QuixBugs 或 SWE-bench 等任务级评测。
