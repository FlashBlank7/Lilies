# implementation_v0.2.66_e08_control_behavior_matrix

## Source

- Source stage report: `docs/stage-reports/v0.2.65_e08_policy_controls_surface.md`
- Source stage task: `Add deterministic E08 control-behavior matrix`; `Restore executable frontend verification when Node is available`; `Preserve complexity-router guarded rollout`
- Current design: `docs/current-design/design_e08_control_behavior_matrix_api.md`; `docs/current-design/design_e08_control_behavior_matrix_evidence.md`; `docs/current-design/design_v0_2_66_frontend_verification_and_complexity_disposition.md`

## Changes

- Added `e08_boundary.behavior_matrix` to the policy-controls API.
- Added rows for workflow passmode, cancellation checkpoint, budget limits, worker lease, network egress policy, and secret policy.
- Added API assertions for matrix enforcement/status semantics.
- Added `scripts/v02_66_e08_control_behavior_matrix.py` to generate deterministic JSON and markdown evidence.
- Retried frontend verification and confirmed the environment blocker remains unchanged.

## Evidence / Intermediate Results

Generated command:

```text
.venv/bin/python scripts/v02_66_e08_control_behavior_matrix.py
```

Generated evidence:

- `docs/workingon/e08_control_behavior_matrix_v0.2.66.json`
- `docs/workingon/e08_control_behavior_matrix_v0.2.66_summary.md`

Matrix rows:

- `workflow_passmode`: workflow-internal, soft configurable.
- `cancellation_checkpoint`: workflow runtime, soft checkpoint.
- `budget_limits`: Platform Harness, hard counter.
- `worker_lease`: Platform Harness, lease coordination.
- `network_egress_policy`: Platform Harness, hard boundary.
- `secret_policy`: Platform Harness, hard boundary.

## Verification

```text
.venv/bin/python -m pytest tests/test_workflow.py -k policy_controls
```

Result:

```text
1 passed, 72 deselected, 1 warning in 0.39s
```

```text
.venv/bin/python -m pytest tests/test_v02_66_e08_control_behavior_matrix.py
```

Result:

```text
1 passed in 0.04s
```

Frontend verification retry:

```text
npm run lint
node_modules/.bin/tsc --noEmit
```

Result:

```text
zsh:1: command not found: npm
env: node: No such file or directory
```

## Remaining Risk

- Frontend executable verification remains blocked by missing `node`/`npm`.
- The matrix is read-only classification; it does not add editable controls.
- Full Platform Harness sidecar completion remains broader than this matrix slice.
- Complexity-router remains deferred until guardrails and rollout design are selected.

## Design Decision

- Continue current design / revise current design / proceed to next design / blocked: proceed to archive
