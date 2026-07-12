# v0.2.125 E08 remaining sidecar architecture reselection

- Raw evidence: `docs/workingon-archives/v0.2.125/decision_v0.2.125_e08_remaining_sidecar_architecture_reselection.json`
- Status: `completed`
- Decision: `select_production_worker_supervision`
- Selected slice: `production_worker_supervision`
- Next version: `v0.2.126_e08_production_worker_supervision`
- First design: `docs/current-design/design_v0_2_126_e08_production_worker_supervision.md`
- Completed builder_build excluded: `True`
- Worker task-kind execution coverage preserved: `True`
- Remaining candidates are architecture-only: `True`
- E08 full sidecar completion claimed: `False`
- Reason: v0.2.124 closed required worker task-kind execution coverage by implementing builder_build. The next highest-value remaining architecture slice is production worker supervision because worker handlers and heartbeat evidence now exist, while distributed queue semantics and external KMS provider integration remain broader or less ready follow-up slices.

## Completed Slices

- `builder_build_worker_offload_handler` via `docs/workingon-archives/v0.2.124/evidence_v0.2.124_e08_builder_build_worker_offload_handler_summary.md`
- `benchmark_worker_offload_handler` via `docs/workingon-archives/v0.2.122/evidence_v0.2.122_e08_benchmark_worker_offload_handler_summary.md`
- `draft_patch_preview_worker_offload_handler` via `docs/workingon-archives/v0.2.120/evidence_v0.2.120_e08_draft_patch_preview_worker_offload_handler_summary.md`
- `test_suite_worker_offload_handler` via `docs/workingon-archives/v0.2.118/evidence_v0.2.118_e08_test_suite_worker_offload_handler_summary.md`
- `workflow_run_worker_offload_handler` via `docs/workingon-archives/v0.2.116/evidence_v0.2.116_e08_workflow_run_worker_offload_handler_summary.md`
- `scheduler_trigger_worker_offload_handler` via `docs/workingon-archives/v0.2.114/evidence_v0.2.114_e08_scheduler_trigger_worker_offload_handler_summary.md`
- `distributed_heartbeat_registry` via `docs/workingon-archives/v0.2.112/evidence_v0.2.112_e08_distributed_heartbeat_registry_summary.md`
- `complete_handler_catalog` via `docs/workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md`
- `stdio_container_egress_allowlist_contract` via `docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md`
- `secret_kms_rotation_contract` via `docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md`
- `editable_policy_controls_api` via `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- `studio_editable_policy_controls` via `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- `operator_runbook_lifecycle` via `docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

## Remaining Architecture Candidates

| Slice | Score | Evidence |
| --- | ---: | --- |
| `production_worker_supervision` | 92 | `docs/stage-report-archives/v0.2.x/v0.2.112_e08_distributed_heartbeat_registry.md; docs/stage-report-archives/v0.2.x/v0.2.124_e08_builder_build_worker_offload_handler.md` |
| `distributed_queue_semantics` | 68 | `docs/stage-report-archives/v0.2.x/v0.2.112_e08_distributed_heartbeat_registry.md; platform/backend/src/agent_platform/harness.py` |
| `external_kms_provider_integration` | 49 | `docs/stage-report-archives/v0.2.x/v0.2.108_e08_secret_kms_rotation_contract.md; platform/backend/src/agent_platform/harness.py` |
