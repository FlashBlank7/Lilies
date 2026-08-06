# v0.2.111 E08 remaining sidecar slice reselection

- Raw evidence: `docs/workingon-archives/v0.2.111/decision_v0.2.111_e08_remaining_sidecar_slice_reselection.json`
- Status: `completed`
- Decision: `select_distributed_heartbeat_registry`
- Selected slice: `distributed_heartbeat_registry`
- Next version: `v0.2.112_e08_distributed_heartbeat_registry`
- First design: `docs/current-design/design_v0_2_112_e08_distributed_heartbeat_registry.md`
- Completed handler catalog excluded: `True`
- E08 full sidecar completion claimed: `False`
- Reason: v0.2.110 closed handler catalog coverage. Among remaining E08 sidecar gaps, the distributed heartbeat registry is the highest-value next slice because worker lease renewal already exists, but worker liveness is still not externally queryable or durable as a registry.

## Completed Slices

- `stdio_container_egress_allowlist_contract` via `docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md`
- `secret_kms_rotation_contract` via `docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md`
- `complete_handler_catalog` via `docs/workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md`
- `editable_policy_controls_api` via `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- `studio_editable_policy_controls` via `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- `operator_runbook_lifecycle` via `docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

## Remaining Candidates

| Slice | Score | Evidence |
| --- | ---: | --- |
| `distributed_heartbeat_registry` | 83 | `docs/stage-report-archives/v0.2.x/v0.2.28_worker_heartbeat_and_renewal.md` |
| `real_worker_offload_handlers` | 59 | `docs/stage-report-archives/v0.2.x/v0.2.110_e08_complete_handler_catalog.md` |
| `long_running_sidecar_operations_beyond_policy_controls` | 49 | `docs/operator-runbooks/e08_policy_controls_operator_runbook.md` |
| `external_kms_provider_integration` | 43 | `docs/stage-report-archives/v0.2.x/v0.2.108_e08_secret_kms_rotation_contract.md` |
