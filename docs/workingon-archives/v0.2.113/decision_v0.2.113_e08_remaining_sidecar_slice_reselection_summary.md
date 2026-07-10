# v0.2.113 E08 remaining sidecar slice reselection

- Raw evidence: `docs/workingon-archives/v0.2.113/decision_v0.2.113_e08_remaining_sidecar_slice_reselection.json`
- Status: `completed`
- Decision: `select_scheduler_trigger_worker_offload_handler`
- Selected slice: `scheduler_trigger_worker_offload_handler`
- Next version: `v0.2.114_e08_scheduler_trigger_worker_offload_handler`
- First design: `docs/current-design/design_v0_2_114_e08_scheduler_trigger_worker_offload_handler.md`
- Completed heartbeat registry excluded: `True`
- E08 full sidecar completion claimed: `False`
- Reason: v0.2.112 closed durable worker heartbeat/liveness registry. The next highest-value concrete slice is a worker-owned scheduler_trigger handler because v0.2.110 exposed scheduler_trigger as an unavailable catalog entry and v0.2.112 now gives worker liveness enough product visibility for a real offload slice.

## Completed Slices

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
| `scheduler_trigger_worker_offload_handler` | 87 | `docs/stage-reports/v0.2.110_e08_complete_handler_catalog.md; docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md` |
| `operational_alerting_for_sidecar_liveness` | 63 | `docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md` |
| `distributed_queue_semantics` | 52 | `docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md` |
| `external_kms_provider_integration` | 43 | `docs/stage-reports/v0.2.108_e08_secret_kms_rotation_contract.md` |
