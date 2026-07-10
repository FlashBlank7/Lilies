# E08 Platform Harness policy controls operator runbook

Status: active_product_runbook

Source stage: `docs/stage-reports/v0.2.100_e08_operator_runbook_lifecycle.md`

Evidence:

- Backend editable API: `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- Studio editable controls: `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- Post-Studio path decision: `docs/workingon-archives/v0.2.99/decision_v0.2.99_e08_post_studio_controls_summary.md`

## Scope

This runbook covers operator lifecycle for E08 Platform Harness policy controls after the backend PATCH API and Studio editable controls are available.

Controls covered:

- network egress policy and allowlist;
- cancellation policy;
- secret policy;
- worker lease seconds;
- task and owner budget limits.

This runbook does not claim full Platform Harness sidecar completion. Broader sidecar boundary closure remains a future stage-report decision.

## Before-Change Checks

1. Confirm the intended change has an operator reason that can be written into the Studio reason field or PATCH request.
2. Read the current policy controls from Studio monitor tab or `GET /api/v1/platform/harness/policy-controls`.
3. Confirm the change is scoped to E08 Platform Harness controls and does not require E07 complexity-router default changes.
4. Check recent Platform Harness task failures for active incidents before tightening network, budget, cancellation, or lease controls.
5. For allowlist changes, normalize host names and avoid URL paths, schemes, ports, or blank entries.
6. For budget changes, confirm whether the limit is task-level or owner-level.
7. For cancellation changes, confirm whether disabling cancellation is temporary and who can approve rollback.

## Apply-Change Procedure

1. Open the Studio monitor tab for the target application.
2. Refresh Platform Harness policy controls.
3. Edit only the required controls.
4. Enter a non-empty reason that names the operational intent.
5. Save the policy controls.
6. Confirm Studio reports a saved status and changed fields.
7. If using API instead of Studio, call `PATCH /api/v1/platform/harness/policy-controls` with the same reason and only the intended mutable fields.

## Post-Change Verification

1. Re-read `GET /api/v1/platform/harness/policy-controls`.
2. Confirm the response `after` state matches the intended values.
3. Confirm the audit block includes the operator reason and changed fields.
4. For `cancellation_policy=disabled`, verify workflow run cancellation is blocked with an operator-visible error.
5. For network allowlist changes, verify disallowed hosts remain blocked and intended hosts are represented as normalized host names.
6. For budget changes, verify the configured task and owner limits are visible in policy controls.
7. Record the evidence path in the stage report or incident note.

## Rollback Procedure

1. Identify the last known good policy state from the previous policy-controls response or stage evidence.
2. Use Studio or `PATCH /api/v1/platform/harness/policy-controls` to restore the previous values.
3. Use a rollback reason that names the triggering symptom.
4. Re-read policy controls and confirm the rollback state.
5. If rollback fails, stop further policy edits and escalate as an incident.

## Incident Escalation

Escalate when any of the following occur:

- policy save fails repeatedly;
- network policy blocks required production traffic;
- budget limits stop legitimate work;
- cancellation is disabled longer than the approved window;
- worker leases expire or block handlers unexpectedly;
- secret policy is disabled during a sensitive operation;
- audit evidence is missing or inconsistent.

Escalation note must include:

- current policy controls response;
- intended change and reason;
- changed fields;
- observed failure or blocked workflow;
- rollback attempt result.

## Evidence Checklist

- [ ] Current policy controls captured.
- [ ] Operator reason recorded.
- [ ] Changed fields recorded.
- [ ] Post-change policy controls captured.
- [ ] Rollback state captured when rollback is required.
- [ ] Incident escalation note captured when escalation is required.

## Product Boundary

This runbook productizes the operator lifecycle for the currently implemented E08 controls. It does not close allowlist-grade stdio/container egress, KMS/rotation, full handler catalog, distributed heartbeat registry, or full long-running sidecar operations.
