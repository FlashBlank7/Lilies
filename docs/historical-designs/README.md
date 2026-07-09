# historical-designs

本目录保存已经具备明确版本 state 的历史 design。

归档规则：

- 只能归档已经出现在 stage report 或等价版本 state 文档中的 design。
- 文件名必须以版本或明确 state 为主，例如 `v0.2.2_design_<topic>.md`。
- 不使用日期作为 historical design 的主文件名。
- 日期、归档时间、执行人等信息可以写入 manifest 或文件内容，但不能替代版本/state。

当前已归档：

- `v0.2.2_*`：来自 `docs/stage-reports/v0.2.2_apply_lilies_inspiration_notes.md`。
- `v0.2.3_*`：来自 `docs/stage-reports/v0.2.3_platform_harness_and_development_roadmap.md`。
- `v0.2.4_*`：来自 `docs/stage-reports/v0.2.4_platform_harness_observability_ui.md`。
- `v0.2.5_*`：来自 `docs/stage-reports/v0.2.5_builder_benchmark_suite.md`。
- `v0.2.6_*`：来自 `docs/stage-reports/v0.2.6_paid_builder_benchmark_experiment.md`。
- `v0.2.7_*`：来自 `docs/stage-reports/v0.2.7_builder_test_self_consistency.md`。
- `v0.2.8_*`：来自 `docs/stage-reports/v0.2.8_paid_builder_benchmark_rerun.md`。
- `v0.2.9_*`：来自 `docs/stage-reports/v0.2.9_benchmark_node_type_equivalence.md`。
- `v0.2.10_*`：来自 `docs/stage-reports/v0.2.10_platform_harness_durable_storage.md`。
- `v0.2.11_*`：来自 `docs/stage-reports/v0.2.11_platform_harness_owner_budget.md`。
- `v0.2.12_*`：来自 `docs/stage-reports/v0.2.12_platform_harness_stale_task_reconciliation.md`。
- `v0.2.13_*`：来自 `docs/stage-reports/v0.2.13_builder_benchmark_history.md`。
- `v0.2.14_*`：来自 `docs/stage-reports/v0.2.14_platform_harness_asset_update.md`。
- `v0.2.15_*`：来自 `docs/stage-reports/v0.2.15_platform_harness_secret_policy.md`。
- `v0.2.16_*`：来自 `docs/stage-reports/v0.2.16_platform_harness_network_egress_policy.md`。
- `v0.2.17_*`：来自 `docs/stage-reports/v0.2.17_platform_harness_tool_egress_policy.md`。
- `v0.2.18_*`：来自 `docs/stage-reports/v0.2.18_evolution_governance_and_workspace_archive.md`。
- `v0.2.19_*`：来自 `docs/stage-reports/v0.2.19_full_task_set_product_visibility.md`。
- `v0.2.20_*`：来自 `docs/stage-reports/v0.2.20_platform_harness_worker_lease.md`。
- `v0.2.21_*`：来自 `docs/stage-reports/v0.2.21_platform_harness_secret_references.md`。
- `v0.2.22_*`：来自 `docs/stage-reports/v0.2.22_platform_harness_stdio_sandbox_egress.md`。
- `v0.2.23_*`：来自 `docs/stage-reports/v0.2.23_sandboxed_stdio_mcp_runner.md`。
- `v0.2.24_*`：来自 `docs/stage-reports/v0.2.24_platform_harness_stdio_policy_controls.md`。
- `v0.2.25_*`：来自 `docs/stage-reports/v0.2.25_platform_harness_secret_envelope.md`。
- `v0.2.26_*`：来自 `docs/stage-reports/v0.2.26_platform_harness_worker_runner.md`。
- `v0.2.27_*`：来自 `docs/stage-reports/v0.2.27_worker_runner_cli_and_handler.md`。
- `v0.2.28_*`：来自 `docs/stage-reports/v0.2.28_worker_heartbeat_and_renewal.md`。
- `v0.2.29_*`：来自 `docs/stage-reports/v0.2.29_formal_experiment_tranche.md`。
- `v0.2.30_*`：来自 `docs/stage-reports/v0.2.30_builder_terminal_node_repair.md`。
- `v0.2.31_*`：来自 `docs/stage-reports/v0.2.31_builder_repair_confirmation.md`。
- `v0.2.32_*`：来自 `docs/stage-reports/v0.2.32_e01_plan_first_ab.md`。
- `v0.2.33_*`：来自 `docs/stage-reports/v0.2.33_e01_complex_ab.md`。
- `v0.2.34_*`：来自 `docs/stage-reports/v0.2.34_e01_required_readiness_repair.md`。
- `v0.2.35_*`：来自 `docs/stage-reports/v0.2.35_e01_required_architecture_coverage.md`。

Active workspace rule:

- `docs/current-design/` must be empty except README after every stage archive.
- Historical design files live here, not in the active design workspace.
