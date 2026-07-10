# v0.2.109 E08 remaining sidecar slice reselection

- Raw evidence: `docs/workingon-archives/v0.2.109/decision_v0.2.109_e08_remaining_sidecar_slice_reselection.json`
- Status: `completed`
- Decision: `select_complete_handler_catalog`
- Selected slice: `complete_handler_catalog`
- Next version: `v0.2.110_e08_complete_handler_catalog`
- First design: `docs/current-design/design_v0_2_110_e08_complete_handler_catalog.md`
- Completed secret slice excluded: `True`
- E08 full sidecar completion claimed: `False`
- Reason: v0.2.108 closed the local secret rotation envelope slice. Among remaining open E08 sidecar gaps, the complete handler catalog is the highest-value next slice because worker-handler coverage is sidecar-critical, implementation-ready from v0.2.27 evidence, and testable without claiming full sidecar completion.

## Completed Slices

- `stdio_container_egress_allowlist_contract` via `docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md`
- `secret_kms_rotation_contract` via `docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md`
- `editable_policy_controls_api` via `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- `studio_editable_policy_controls` via `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- `operator_runbook_lifecycle` via `docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

## Remaining Candidates

| Slice | Score | Evidence |
| --- | ---: | --- |
| `complete_handler_catalog` | 87 | `docs/stage-reports/v0.2.27_worker_runner_cli_and_handler.md` |
| `distributed_heartbeat_registry` | 64 | `docs/stage-reports/v0.2.28_worker_heartbeat_and_renewal.md` |
| `long_running_sidecar_operations_runbook` | 50 | `docs/operator-runbooks/e08_policy_controls_operator_runbook.md` |
| `external_kms_provider_integration` | 43 | `docs/stage-reports/v0.2.108_e08_secret_kms_rotation_contract.md` |
