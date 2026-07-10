# Lilies docs index

本目录采用文档驱动开发结构。入口顺序如下：

1. `PROJECT_EVOLUTION_STRATEGY.md`：文档分层、阶段归档和智力资产筛选规范。
2. `stage-reports/`：小版本阶段报告，采用 compact factsheet，说明每个 stage 完成了什么、留下什么任务。
3. `intellectual-assets/`：少而精的可复用结论，供后续 design 和 plan 引用。
4. `experiment-status/`：当前版本实验闭环、已应用标记和原始实验 backlog 关闭状态。默认读取顺序是索引 -> ledger -> `*_summary.md` -> raw JSON。
5. `workingon/`：当前 stage 的 active 中间结果工作区，归档后只保留 README。
6. `operator-runbooks/`：稳定的操作员流程与运行手册，必须由 stage report 归档引用。
7. `workingon-archives/`：已归档的小版本 workingon 中间材料。
8. `current-design/`：当前 stage 的 active design 工作区，归档后只保留 README。
9. `historical-designs/`：已经具备明确版本 state 的历史 design，只保存最终设计契约。
10. `phase-reports/`：大版本完成后的总复盘和路线转向。
11. `source-materials/`：早期长报告、会议材料、论文草稿和历史证据链。

## 当前稳定资产

- `intellectual-assets/asset_blockflow_language_system.md`
- `intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `intellectual-assets/asset_harness_llm_composite.md`
- `intellectual-assets/asset_lilies_competitive_strategy.md`
- `intellectual-assets/asset_adaptive_reuse_defaultization_gate.md`
- `intellectual-assets/asset_adaptive_default_live_acceptance_boundary.md`

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
- `stage-reports/v0.2.40_builder_provider_timeout_boundary.md`
- `stage-reports/v0.2.41_e05_success_condition_after_timeout_boundary.md`
- `stage-reports/v0.2.42_builder_build_level_watchdog.md`
- `stage-reports/v0.2.43_e05_multifamily_with_build_watchdog.md`
- `stage-reports/v0.2.44_customer_support_template_reuse_repair.md`
- `stage-reports/v0.2.45_customer_support_e05_repair_rerun.md`
- `stage-reports/v0.2.46_deep_reuse_deadline_governance.md`
- `stage-reports/v0.2.47_shallow_reuse_breadth_validation.md`
- `stage-reports/v0.2.48_adaptive_reuse_depth_policy.md`
- `stage-reports/v0.2.49_adaptive_policy_live_validation.md`
- `stage-reports/v0.2.50_builder_deadline_visibility.md`
- `stage-reports/v0.2.51_e05_second_family_adaptive_validation.md`
- `stage-reports/v0.2.52_adaptive_default_productization.md`
- `stage-reports/v0.2.53_adaptive_default_live_acceptance.md`
- `stage-reports/v0.2.54_policy_default_live_reliability.md`
- `stage-reports/v0.2.55_e08_harness_sidecar_passmode.md`
- `stage-reports/v0.2.56_adaptive_long_term_monitoring.md`
- `stage-reports/v0.2.57_full_backlog_closure.md`
- `stage-reports/v0.2.58_continuous_auto_evolution.md`
- `stage-reports/v0.2.59_productization_lane_selection.md`
- `stage-reports/v0.2.60_adaptive_monitoring_product_surface.md`
- `stage-reports/v0.2.61_adaptive_monitoring_refresh_control.md`
- `stage-reports/v0.2.62_evolution_process_architecture.md`
- `stage-reports/v0.2.63_adaptive_monitoring_schedule_and_report_audit.md`
- `stage-reports/v0.2.64_productization_lane_reselection.md`
- `stage-reports/v0.2.65_e08_policy_controls_surface.md`
- `stage-reports/v0.2.66_e08_control_behavior_matrix.md`
- `stage-reports/v0.2.67_e08_full_boundary_gap_selection.md`
- `stage-reports/v0.2.68_e08_cancellation_budget_behavior.md`
- `stage-reports/v0.2.69_e08_continuation_decision.md`
- `stage-reports/v0.2.70_complexity_router_guardrail_selection.md`
- `stage-reports/v0.2.71_complexity_router_default_safety_gate.md`
- `stage-reports/v0.2.72_complexity_router_requirement_classification_contract.md`
- `stage-reports/v0.2.73_complexity_router_operator_override_plan.md`
- `stage-reports/v0.2.74_complexity_router_rollout_metrics_prerequisites.md`
- `stage-reports/v0.2.75_complexity_router_default_enablement_boundary.md`
- `stage-reports/v0.2.76_complexity_router_live_validation_plan.md`
- `stage-reports/v0.2.77_complexity_router_live_validation_execution_decision.md`
- `stage-reports/v0.2.78_complexity_router_bounded_live_validation.md`
- `stage-reports/v0.2.79_complexity_router_default_enablement_review_decision.md`
- `stage-reports/v0.2.80_complexity_router_staged_rollout_preparation.md`
- `stage-reports/v0.2.81_complexity_router_staged_rollout_execution_decision.md`
- `stage-reports/v0.2.82_complexity_router_shadow_only_rollout.md`
- `stage-reports/v0.2.83_complexity_router_post_shadow_rollout_decision.md`
- `stage-reports/v0.2.84_complexity_router_operator_opt_in_rollout.md`
- `stage-reports/v0.2.85_complexity_router_post_operator_opt_in_decision.md`
- `stage-reports/v0.2.86_frontend_verification_environment_repair.md`
- `stage-reports/v0.2.87_complexity_router_default_enablement_review_decision.md`
- `stage-reports/v0.2.88_complexity_router_limited_default_enablement_plan.md`
- `stage-reports/v0.2.89_complexity_router_limited_default_enablement_contract.md`
- `stage-reports/v0.2.90_complexity_router_runtime_activation_path.md`
- `stage-reports/v0.2.91_complexity_router_runtime_activation_observability.md`
- `stage-reports/v0.2.92_complexity_router_limited_default_readiness_review.md`
- `stage-reports/v0.2.93_complexity_router_guarded_default_rollout.md`
- `stage-reports/v0.2.94_productization_lane_reselection.md`
- `stage-reports/v0.2.95_e08_followup_controls_scope.md`
- `stage-reports/v0.2.96_e08_editable_policy_controls_api.md`
- `stage-reports/v0.2.97_e08_post_api_productization_decision.md`
- `stage-reports/v0.2.98_e08_studio_editable_policy_controls.md`
- `stage-reports/v0.2.99_e08_post_studio_controls_decision.md`
- `stage-reports/v0.2.100_e08_operator_runbook_lifecycle.md`
- `stage-reports/v0.2.101_e08_post_runbook_disposition.md`
- `stage-reports/v0.2.102_productization_lane_reselection.md`
- `stage-reports/v0.2.103_e05_scheduled_monitoring_hook.md`
- `stage-reports/v0.2.104_productization_lane_reselection.md`
- `stage-reports/v0.2.105_e08_broader_sidecar_scope_decomposition.md`
- `stage-reports/v0.2.106_e08_stdio_container_egress_allowlist_contract.md`
- `stage-reports/v0.2.107_e08_remaining_sidecar_slice_reselection.md`
- `stage-reports/v0.2.108_e08_secret_kms_rotation_contract.md`
- `stage-reports/v0.2.109_e08_remaining_sidecar_slice_reselection.md`
- `stage-reports/v0.2.110_e08_complete_handler_catalog.md`
- `stage-reports/v0.2.111_e08_remaining_sidecar_slice_reselection.md`
- `stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md`
- `stage-reports/v0.2.113_e08_remaining_sidecar_slice_reselection.md`
- `stage-reports/v0.2.114_e08_scheduler_trigger_worker_offload_handler.md`
- `stage-reports/v0.2.115_e08_remaining_sidecar_slice_reselection.md`

## 使用原则

新任务先由最新 `stage-reports/` 的 next-stage task set 确定；当前执行中间材料进入 `workingon/`；需要展开的具体实现计划进入 `current-design/`；只有已经具备明确版本 state 的 design 才归入 `historical-designs/`；阶段结束后归档到 `stage-reports/`；只有经过筛选的复杂结论才进入 `intellectual-assets/`。

实验状态必须维护在 `experiment-status/`。active `workingon/experiment-*` 只保存进行中的实验过程；完成后的 DOCX 报告和 raw evidence 进入 `experiment-status/`，不能替代实验闭环台账。已经用于工程改进的实验必须标记 `已应用` 或 `验证应用`，并补充证据链。常规读取优先索引、单实验 ledger 和 `evidence/*_summary.md`，只有争议或缺字段时再读 raw JSON。

归档后，`current-design/` 和 `workingon/` 必须只保留 README。旧 design 与中间材料分别进入 `historical-designs/` 和 `workingon-archives/`。

## 强制边界

- 下一阶段任务只来自最新 `stage-reports/` 的 `Next-stage Task Set`。
- `workingon/` 只保存中间结果、实现证据、实验过程或临时分析；不得作为任务拆解或版本推进的权威来源。
- 新 stage report 必须使用 `stage-reports/STAGE_REPORT_TEMPLATE.md` 的固定 section；没有内容也要写 `none`。
- 小版本推进必须有明确 scope justification；连续出现“一个版本只有一个 historical design”应视为阶段切分问题，而不是正常节奏。
