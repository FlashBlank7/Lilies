# Lilies docs index

本目录采用文档驱动开发结构。入口顺序如下：

1. `PROJECT_EVOLUTION_STRATEGY.md`：文档分层、阶段归档和智力资产筛选规范。
2. `stage-reports/`：小版本阶段报告，说明每个 stage 完成了什么、留下什么任务。
3. `intellectual-assets/`：少而精的可复用结论，供后续 design 和 plan 引用。
4. `experiment-status/`：当前版本实验闭环、已应用标记和原始实验 backlog 关闭状态。
5. `workingon/`：当前 stage 的 active 中间结果工作区，归档后只保留 README。
6. `workingon-archives/`：已归档的小版本 workingon 中间材料。
7. `current-design/`：当前 stage 的 active design 工作区，归档后只保留 README。
8. `historical-designs/`：已经具备明确版本 state 的历史 design。
9. `phase-reports/`：大版本完成后的总复盘和路线转向。
10. `source-materials/`：早期长报告、会议材料、论文草稿和历史证据链。

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
- `stage-reports/v0.2.14_platform_harness_asset_update.md`
- `stage-reports/v0.2.15_platform_harness_secret_policy.md`
- `stage-reports/v0.2.16_platform_harness_network_egress_policy.md`
- `stage-reports/v0.2.17_platform_harness_tool_egress_policy.md`
- `stage-reports/v0.2.18_evolution_governance_and_workspace_archive.md`
- `stage-reports/v0.2.19_full_task_set_product_visibility.md`
- `stage-reports/v0.2.20_platform_harness_worker_lease.md`
- `stage-reports/v0.2.21_platform_harness_secret_references.md`
- `stage-reports/v0.2.22_platform_harness_stdio_sandbox_egress.md`
- `stage-reports/v0.2.23_sandboxed_stdio_mcp_runner.md`
- `stage-reports/v0.2.24_platform_harness_stdio_policy_controls.md`
- `stage-reports/v0.2.25_platform_harness_secret_envelope.md`
- `stage-reports/v0.2.26_platform_harness_worker_runner.md`
- `stage-reports/v0.2.27_worker_runner_cli_and_handler.md`
- `stage-reports/v0.2.28_worker_heartbeat_and_renewal.md`
- `stage-reports/v0.2.29_formal_experiment_tranche.md`
- `stage-reports/v0.2.30_builder_terminal_node_repair.md`
- `stage-reports/v0.2.31_builder_repair_confirmation.md`
- `stage-reports/v0.2.32_e01_plan_first_ab.md`
- `stage-reports/v0.2.33_e01_complex_ab.md`
- `stage-reports/v0.2.34_e01_required_readiness_repair.md`
- `stage-reports/v0.2.35_e01_required_architecture_coverage.md`
- `stage-reports/v0.2.36_e02_readable_testframe_human_review.md`
- `stage-reports/v0.2.37_e04_local_repair_vs_full_rebuild.md`
- `stage-reports/v0.2.38_e05_template_reuse_depth_live_comparison.md`
- `stage-reports/v0.2.39_template_reuse_expandability_contract.md`

## 使用原则

新任务先由最新 `stage-reports/` 的 next-stage task set 确定；当前执行中间材料进入 `workingon/`；需要展开的具体实现计划进入 `current-design/`；只有已经具备明确版本 state 的 design 才归入 `historical-designs/`；阶段结束后归档到 `stage-reports/`；只有经过筛选的复杂结论才进入 `intellectual-assets/`。

实验状态必须维护在 `experiment-status/`。active `workingon/experiment-*` 只保存进行中的实验过程；完成后的 DOCX 报告和 raw evidence 进入 `experiment-status/`，不能替代实验闭环台账。已经用于工程改进的实验必须标记 `已应用` 或 `验证应用`，并补充证据链。

归档后，`current-design/` 和 `workingon/` 必须只保留 README。旧 design 与中间材料分别进入 `historical-designs/` 和 `workingon-archives/`。
