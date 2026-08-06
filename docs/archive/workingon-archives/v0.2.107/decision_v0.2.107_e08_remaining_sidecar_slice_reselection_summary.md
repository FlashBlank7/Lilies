# v0.2.107 E08 remaining sidecar slice reselection

- Raw evidence: `docs/workingon-archives/v0.2.107/decision_v0.2.107_e08_remaining_sidecar_slice_reselection.json`
- Status: `completed`
- Decision: `select_secret_kms_rotation_contract`
- Selected slice: `secret_kms_rotation_contract`
- Next version: `v0.2.108_e08_secret_kms_rotation_contract`
- First design: `docs/current-design/design_v0_2_108_e08_secret_kms_rotation_contract.md`
- E08 full sidecar completion claimed: `False`
- Reason: The stdio/container egress slice is complete. Among remaining E08 sidecar gaps, the KMS/rotation-grade secret envelope contract is the highest-value next slice because secret persistence is sidecar-critical, has prior policy/envelope evidence, and is testable without claiming full sidecar completion.

## Completed Slices

- `stdio_container_egress_allowlist_contract` via `docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md`
- `editable_policy_controls_api` via `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- `studio_editable_policy_controls` via `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- `operator_runbook_lifecycle` via `docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

## Remaining Candidates

| Slice | Score | Evidence |
| --- | ---: | --- |
| `secret_kms_rotation_contract` | 90 | `docs/stage-report-archives/v0.2.x/v0.2.15_platform_harness_secret_policy.md; docs/stage-report-archives/v0.2.x/v0.2.25_platform_harness_secret_envelope.md` |
| `complete_handler_catalog` | 74 | `docs/stage-report-archives/v0.2.x/v0.2.27_worker_runner_cli_and_handler.md` |
| `distributed_heartbeat_registry` | 60 | `docs/stage-report-archives/v0.2.x/v0.2.28_worker_heartbeat_and_renewal.md` |
| `long_running_sidecar_operations_runbook` | 50 | `docs/operator-runbooks/e08_policy_controls_operator_runbook.md` |
