# implementation_v0.2.80_complexity_router_staged_rollout_preparation

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.79_complexity_router_default_enablement_review_decision.md`
- Source stage task: `Implement complexity-router staged rollout preparation`; `Preserve default-disabled status`; `Restore executable frontend verification when Node is available`
- Current design: `docs/current-design/design_complexity_router_staged_rollout_preparation.md`; `docs/current-design/design_v0_2_80_operator_controls_and_rollback.md`; `docs/current-design/design_v0_2_80_default_disabled_and_frontend_blocker.md`

## Changes

- Added staged rollout preparation generator.
- Defined three non-default stages: shadow-only, operator opt-in, and limited default review readiness.
- Defined operator controls and rollback criteria.
- Preserved `default_enabled=false`.
- Retried frontend verification and recorded the unchanged Node/npm blocker.

## Evidence / Intermediate Results

- `docs/workingon/rollout_v0.2.80_complexity_router_staged_preparation.json`
- `docs/workingon/rollout_v0.2.80_complexity_router_staged_preparation_summary.md`

## Verification

```text
.venv/bin/python -m pytest tests/test_v02_80_complexity_router_staged_rollout_preparation.py tests/test_v02_79_complexity_router_default_enablement_review_decision.py tests/test_complexity_router_default_safety.py
```

Result:

```text
14 passed, 1 warning in 0.43s
```

Frontend verification retry:

```text
zsh:1: command not found: npm
env: node: No such file or directory
```

## Remaining Risk

- Staged rollout is prepared but not executed.
- Default router behavior remains disabled.
- Frontend executable verification remains blocked.

## Design Decision

- Continue current design / revise current design / proceed to next design / blocked: proceed to archive
