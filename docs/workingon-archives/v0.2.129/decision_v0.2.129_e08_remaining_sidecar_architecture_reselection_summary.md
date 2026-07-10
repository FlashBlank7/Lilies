# v0.2.129 E08 remaining sidecar architecture reselection

- Raw evidence: `docs/workingon-archives/v0.2.129/decision_v0.2.129_e08_remaining_sidecar_architecture_reselection.json`
- Status: `completed`
- Decision: `select_external_process_manager`
- Selected slice: `external_process_manager`
- Next version: `v0.2.130_e08_external_process_manager`
- First design: `docs/current-design/design_v0_2_130_e08_external_process_manager.md`
- Completed distributed queue excluded: `True`
- Completed production supervision excluded: `True`
- Worker task-kind execution coverage preserved: `True`
- Remaining candidates are architecture-only: `True`
- E08 full sidecar completion claimed: `False`
- Reason: v0.2.128 closed storage-backed distributed queue semantics. The next highest-value remaining architecture slice is an external worker process manager because supervision and queue ownership now exist inside the app, but process-level spawn/observe/stop/restart semantics are still missing. External KMS provider integration remains an important security slice but is less coupled to the current worker-sidecar execution path.

## Completed Slices

- `distributed_queue_semantics` via `docs/workingon-archives/v0.2.128/evidence_v0.2.128_e08_distributed_queue_semantics_summary.md`
- `production_worker_supervision` via `docs/workingon-archives/v0.2.126/evidence_v0.2.126_e08_production_worker_supervision_summary.md`
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
| `external_process_manager` | 74 | `docs/stage-reports/v0.2.126_e08_production_worker_supervision.md; docs/stage-reports/v0.2.128_e08_distributed_queue_semantics.md; platform/backend/src/agent_platform/worker_runner.py` |
| `external_kms_provider_integration` | 52 | `docs/stage-reports/v0.2.108_e08_secret_kms_rotation_contract.md; platform/backend/src/agent_platform/platform_harness.py` |
