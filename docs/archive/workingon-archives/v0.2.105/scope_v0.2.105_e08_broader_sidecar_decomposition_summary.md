# v0.2.105 E08 broader sidecar scope decomposition

- Raw evidence: `docs/workingon-archives/v0.2.105/scope_v0.2.105_e08_broader_sidecar_decomposition.json`
- Status: `completed`
- Decision: `select_stdio_container_egress_allowlist_contract`
- Selected slice: `stdio_container_egress_allowlist_contract`
- Next version: `v0.2.106_e08_stdio_container_egress_allowlist_contract`
- First design: `docs/current-design/design_v0_2_106_e08_stdio_container_egress_allowlist_contract.md`
- E08 full sidecar completion claimed: `False`
- Reason: Allowlist-grade stdio/container egress is the highest-scoring concrete E08 sidecar slice: it is sidecar-critical, has prior stdio/sandbox evidence, is testable without claiming full sidecar completion, and does not duplicate the completed API/Studio/runbook tranche.

## Completed Current Tranche

- `sidecar_passmode_deterministic_comparison` via `docs/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10_summary.md`
- `editable_policy_controls_api` via `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- `studio_editable_policy_controls` via `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- `operator_runbook_lifecycle` via `docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

## Remaining Gap Candidates

| Slice | Score | Existing evidence |
| --- | ---: | --- |
| `stdio_container_egress_allowlist_contract` | 108 | `docs/stage-report-archives/v0.2.x/v0.2.22_platform_harness_stdio_sandbox_egress.md; docs/stage-report-archives/v0.2.x/v0.2.24_platform_harness_stdio_policy_controls.md` |
| `secret_kms_rotation_contract` | 76 | `docs/stage-report-archives/v0.2.x/v0.2.15_platform_harness_secret_policy.md; docs/stage-report-archives/v0.2.x/v0.2.25_platform_harness_secret_envelope.md` |
| `complete_handler_catalog` | 70 | `docs/stage-report-archives/v0.2.x/v0.2.27_worker_runner_cli_and_handler.md` |
| `distributed_heartbeat_registry` | 56 | `docs/stage-report-archives/v0.2.x/v0.2.28_worker_heartbeat_and_renewal.md` |
| `long_running_sidecar_operations_runbook` | 50 | `docs/operator-runbooks/e08_policy_controls_operator_runbook.md` |
