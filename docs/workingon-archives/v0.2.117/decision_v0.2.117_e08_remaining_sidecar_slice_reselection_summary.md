# v0.2.117 E08 remaining sidecar slice reselection

- Raw evidence: `docs/workingon-archives/v0.2.117/decision_v0.2.117_e08_remaining_sidecar_slice_reselection.json`
- Status: `completed`
- Decision: `select_test_suite_worker_offload_handler`
- Selected slice: `test_suite_worker_offload_handler`
- Next version: `v0.2.118_e08_test_suite_worker_offload_handler`
- First design: `docs/current-design/design_v0_2_118_e08_test_suite_worker_offload_handler.md`
- Completed workflow_run excluded: `True`
- E08 full sidecar completion claimed: `False`
- Reason: v0.2.116 closed the core workflow_run worker offload path. The next highest-value concrete sidecar slice is a worker-owned test_suite handler because it extends worker ownership to validation, is highly testable with existing workflow test machinery, and is lower risk than builder_build or production supervision before more worker-owned handlers exist.

## Completed Slices

- `workflow_run_worker_offload_handler` via `docs/workingon-archives/v0.2.116/evidence_v0.2.116_e08_workflow_run_worker_offload_handler_summary.md`
- `scheduler_trigger_worker_offload_handler` via `docs/workingon-archives/v0.2.114/evidence_v0.2.114_e08_scheduler_trigger_worker_offload_handler_summary.md`
- `distributed_heartbeat_registry` via `docs/workingon-archives/v0.2.112/evidence_v0.2.112_e08_distributed_heartbeat_registry_summary.md`
- `complete_handler_catalog` via `docs/workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md`
- `stdio_container_egress_allowlist_contract` via `docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md`
- `secret_kms_rotation_contract` via `docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md`
- `editable_policy_controls_api` via `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- `studio_editable_policy_controls` via `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- `operator_runbook_lifecycle` via `docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

## Remaining Candidates

| Slice | Score | Evidence |
| --- | ---: | --- |
| `test_suite_worker_offload_handler` | 89 | `platform/backend/src/agent_platform/workflow_runtime.py; platform/backend/src/agent_platform/workflow_storage.py; tests/test_workflow.py` |
| `draft_patch_preview_worker_offload_handler` | 68 | `platform/backend/src/agent_platform/draft_patch_preview.py` |
| `builder_build_worker_offload_handler` | 65 | `platform/backend/src/agent_platform/builder.py; docs/experiment-status/ledgers/builder_benchmark_foundation.md` |
| `benchmark_worker_offload_handler` | 60 | `platform/backend/src/agent_platform/builder_benchmark.py` |
| `production_worker_supervision` | 59 | `docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md; docs/stage-reports/v0.2.116_e08_workflow_run_worker_offload_handler.md` |
| `distributed_queue_semantics` | 52 | `docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md` |
