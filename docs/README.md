# Lilies docs index

本目录采用文档驱动开发结构。入口顺序如下：

1. `PROJECT_EVOLUTION_STRATEGY.md`：文档分层、阶段归档和智力资产筛选规范。
2. `stage-reports/`：小版本阶段报告，说明每个 stage 完成了什么、留下什么任务。
3. `intellectual-assets/`：少而精的可复用结论，供后续 design 和 plan 引用。
4. `workingon/`：当前 stage 的任务级 plan、中间实验和临时结果。
5. `current-design/`：当前任务的具体设计和实现报告。
6. `historical-designs/`：已经具备明确版本 state 的历史 design。
7. `phase-reports/`：大版本完成后的总复盘和路线转向。
8. `source-materials/`：早期长报告、会议材料、论文草稿和历史证据链。

## 当前稳定资产

- `intellectual-assets/asset_blockflow_language_system.md`
- `intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `intellectual-assets/asset_harness_llm_composite.md`
- `intellectual-assets/asset_lilies_competitive_strategy.md`

## 当前阶段报告

- `stage-reports/v0.2.1_docs_consolidation_and_asset_baseline.md`
- `stage-reports/v0.2.2_apply_lilies_inspiration_notes.md`
- `stage-reports/v0.2.3_platform_harness_and_development_roadmap.md`
- `stage-reports/v0.2.4_platform_harness_observability_ui.md`
- `stage-reports/v0.2.5_builder_benchmark_suite.md`
- `stage-reports/v0.2.6_paid_builder_benchmark_experiment.md`
- `stage-reports/v0.2.7_builder_test_self_consistency.md`
- `stage-reports/v0.2.8_paid_builder_benchmark_rerun.md`
- `stage-reports/v0.2.9_benchmark_node_type_equivalence.md`
- `stage-reports/v0.2.10_platform_harness_durable_storage.md`
- `stage-reports/v0.2.11_platform_harness_owner_budget.md`
- `stage-reports/v0.2.12_platform_harness_stale_task_reconciliation.md`
- `stage-reports/v0.2.13_builder_benchmark_history.md`

## 使用原则

新任务先进入未来的 `workingon/` task plan；需要审阅的实现细节进入未来的 `current-design/`；只有已经具备明确版本 state 的 design 才归入 `historical-designs/`；阶段结束后归档到 `stage-reports/`；只有经过筛选的复杂结论才进入 `intellectual-assets/`。
