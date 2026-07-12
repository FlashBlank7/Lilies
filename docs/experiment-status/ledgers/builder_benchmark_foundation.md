# Builder Benchmark Foundation Ledger

状态：已应用 / 验证应用

## 结论

v0.2.6-v0.2.9 形成了 Builder benchmark 的第一条实验驱动工程闭环：付费 Builder benchmark 暴露测试自洽问题，后续 rerun 又暴露 `model_turn`/`llm` 和 terminal-node 语义等价问题。修复后，benchmark 能更准确判断 BlockFlow 结构覆盖。

## 证据

| 项目 | 路径 |
| --- | --- |
| 初始实验报告 | `../reports/2026-07-09_paid_builder_benchmark_experiment.docx` |
| 初始 summary | `../evidence/experiment_paid_builder_benchmark_result_2026_07_09_summary.md` |
| 自洽 rerun 报告 | `../reports/2026-07-09_builder_test_self_consistency_rerun.docx` |
| 自洽 rerun summary | `../evidence/experiment_builder_test_self_consistency_rerun_2026_07_09_summary.md` |
| 节点等价 recheck 报告 | `../reports/2026-07-09_benchmark_node_type_equivalence_recheck.docx` |
| 节点等价 summary | `../evidence/experiment_benchmark_node_type_equivalence_recheck_2026_07_09_summary.md` |
| Stage chain | `../stage-report-archives/v0.2.x/v0.2.6_paid_builder_benchmark_experiment.md` 到 `../stage-report-archives/v0.2.x/v0.2.9_benchmark_node_type_equivalence.md` |

## 工程应用

- `builder.py`：拒绝测试要求不存在节点类型的自相矛盾测试。
- `builder_benchmark.py`：加入节点类型语义等价。
- 回归测试覆盖：测试自洽、`llm`/`model_turn` 等价、terminal alias。

## 边界

该链路证明 benchmark 本身更可靠，不证明 Builder Team 对所有复杂需求都能生成高质量 BlockFlow。
