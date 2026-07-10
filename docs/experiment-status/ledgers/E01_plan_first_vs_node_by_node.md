# E01 Plan-first vs Node-by-node Ledger

状态：completed_with_conditional_policy；全局默认仍需 complexity router/product rollout

## 当前结论

简单摘要任务中，强制 plan-first 没有质量收益且成本更高。复杂 `complex_research_brief` 中，plan-first required 对结构完整性有明显正向信号；经过 repair budget、显式架构契约和 invalid JSON recoverability 修复后，单个复杂 required case 达到 `ready + benchmark passed`。但这还不足以证明所有复杂任务都应强制 plan-first。

## 关键证据

| 切片 | 标记 | 报告 | 默认摘要 |
| --- | --- | --- | --- |
| paid baseline | 已应用窄修复 | `../reports/2026-07-09_1809_E01_plan_first_paid_builder_baseline.docx` | `../evidence/experiment_v0.2.29_paid_builder_tranche_2026_07_09_summary.md` |
| terminal-node repair | 已应用 | `../../stage-reports/v0.2.30_builder_terminal_node_repair.md` | `../evidence/experiment_v0.2.30_terminal_node_recheck_2026_07_09_summary.md` |
| repair confirmation | 已应用 | `../../stage-reports/v0.2.31_builder_repair_confirmation.md` | `../evidence/experiment_v0.2.31_paid_builder_repair_confirmation_2026_07_09_summary.md` |
| smoke A/B | 第一批完成 | `../reports/2026-07-09_1834_E01_plan_first_vs_node_by_node_ab.docx` | `../evidence/experiment_v0.2.32_e01_plan_first_required_2026_07_09_summary.md` |
| complex A/B | 完成但未应用 | `../reports/2026-07-09_1956_E01_complex_plan_first_vs_node_by_node_ab.docx` | `../evidence/experiment_v0.2.33_e01_complex_required_2026_07_09_summary.md` |
| repair budget | 验证应用 | `../reports/2026-07-09_1910_E01_required_repair_budget_rerun.docx` | `../evidence/experiment_v0.2.34_e01_complex_required_repair_budget_retry_2026_07_09_summary.md` |
| architecture coverage | 验证应用 | `../reports/2026-07-09_1944_E01_required_architecture_coverage_after_json_recovery.docx` | `../evidence/experiment_v0.2.35_e01_required_architecture_coverage_after_json_recovery_2026_07_09_summary.md` |

## 应用记录

- 已应用：terminal `answer -> end` benchmark alias。
- 已应用：repair confirmation boundary，允许 draft/test 修改后确认测试。
- 验证应用：复杂 required case 的架构契约和 malformed tool JSON recoverability。

## 下一步

v0.2.57 final disposition：Plan-first should be conditional: avoid it for simple tasks, require it for complex tasks with architecture coverage needs. 后续若产品化，需要 complexity router 或 architecture requirement 触发规则，而不是全局强制 plan-first。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`
