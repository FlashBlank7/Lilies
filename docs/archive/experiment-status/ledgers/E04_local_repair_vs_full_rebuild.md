# E04 Local Repair vs Full Rebuild Ledger

状态：completed_with_strategy_boundary

## 当前结论

对于已有 BlockFlow 大体正确、失败点能定位到单节点配置的局部错误，local repair 明显更低成本且足够修复。该结论不能外推到架构级错误、多节点耦合错误或需求理解错误。

## 证据

| 项目 | 路径 |
| --- | --- |
| 工程证据审查报告 | `../reports/2026-07-09_1809_E04_local_repair_vs_rebuild_evidence_review.docx` |
| 对照实验报告 | `../reports/2026-07-09_2021_E04_local_repair_vs_full_rebuild.docx` |
| 默认摘要 | `../evidence/experiment_v0.2.37_e04_local_repair_vs_full_rebuild_2026_07_09_summary.md` |
| raw evidence | `../evidence/experiment_v0.2.37_e04_local_repair_vs_full_rebuild_2026_07_09.json` |
| stage report | `../stage-report-archives/v0.2.x/v0.2.37_e04_local_repair_vs_full_rebuild.md` |

## 关键结果

- local repair：1 个 draft op，约 `0.015s`，post-test passed。
- paid full rebuild：也成功，约 `57.127s`，`15` model calls，`22` tool calls。
- narrow conclusion：局部可定位失败优先 local repair。

## 应用记录

标记：验证应用。可用于支持“测试结果反向微调工作流”的工程方向，但不能替代 full rebuild 在架构级失败中的价值。

## 下一步

v0.2.57 final disposition：Local repair is preferred only for isolated node failures; coupled failures need subgraph repair and misunderstood requirements need replan/full rebuild. 这关闭原始策略边界，但不把 local repair 写成所有失败的默认策略。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`
