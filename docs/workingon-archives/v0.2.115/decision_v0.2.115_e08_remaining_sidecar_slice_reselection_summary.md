# v0.2.115 E08 remaining sidecar slice reselection

- Raw evidence: `docs/workingon-archives/v0.2.115/decision_v0.2.115_e08_remaining_sidecar_slice_reselection.json`
- Status: `completed`
- Decision: `select_workflow_run_worker_offload_handler`
- Selected slice: `workflow_run_worker_offload_handler`
- Next version: `v0.2.116_e08_workflow_run_worker_offload_handler`
- First design: `docs/current-design/design_v0_2_116_e08_workflow_run_worker_offload_handler.md`
- Completed scheduler_trigger excluded: `True`
- E08 full sidecar completion claimed: `False`
- Reason: v0.2.114 closed automatic scheduler offload. The next highest-value concrete sidecar slice is a worker-owned workflow_run handler because workflow execution is the core runtime path behind broader worker ownership, has existing runtime/API semantics to reuse, and is more central than benchmark, draft preview, or process-supervision work before more real handlers exist.

## Completed Slices

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
| `workflow_run_worker_offload_handler` | 90 | `docs/stage-reports/v0.2.114_e08_scheduler_trigger_worker_offload_handler.md; platform/backend/src/agent_platform/workflow_runtime.py` |
| `test_suite_worker_offload_handler` | 74 | `platform/backend/src/agent_platform/workflow_runtime.py; platform/backend/src/agent_platform/workflow_storage.py` |
| `draft_patch_preview_worker_offload_handler` | 65 | `platform/backend/src/agent_platform/draft_patch_preview.py` |
| `builder_build_worker_offload_handler` | 60 | `platform/backend/src/agent_platform/builder.py; docs/experiment-status/ledgers/builder_benchmark_foundation.md` |
| `benchmark_worker_offload_handler` | 57 | `platform/backend/src/agent_platform/builder_benchmark.py` |
| `production_worker_supervision` | 55 | `docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md; docs/stage-reports/v0.2.114_e08_scheduler_trigger_worker_offload_handler.md` |
| `distributed_queue_semantics` | 52 | `docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md` |
