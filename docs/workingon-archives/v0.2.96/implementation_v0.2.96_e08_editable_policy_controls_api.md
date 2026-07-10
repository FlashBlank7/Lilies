# v0.2.96 E08 editable policy-controls API implementation

## Summary

v0.2.96 implemented the E08 editable policy-controls API selected by `docs/stage-reports/v0.2.95_e08_followup_controls_scope.md`.

## What changed

- Added `PATCH /api/v1/platform/harness/policy-controls`.
- Added `PlatformHarness.update_policy_controls()`.
- Added mutable controls for:
  - `network_egress_policy`
  - `network_egress_allowlist`
  - `cancellation_policy`
  - `secret_policy_enabled`
  - `worker_lease_seconds`
  - task and owner budget `limits`
- Added `PlatformHarness.enforce_cancellation_policy()` and wired it into workflow run cancellation.
- Added backend API tests and v0.2.96 evidence tests.
- Generated before/after evidence.

## Verification

- `.venv/bin/python -m pytest tests/test_workflow.py -k 'policy_controls' tests/test_v02_96_e08_editable_policy_controls_api.py -q` -> `4 passed, 71 deselected, 1 warning`
- `.venv/bin/python scripts/v02_96_e08_editable_policy_controls_api.py` -> `platform_harness.policy_controls.updated`

## Boundary

This version does not claim full Platform Harness sidecar completion. Studio editable controls UI and long-running operator runbook remain future choices through stage reports.

## E07 invariant

No E07 complexity-router code, defaults, tests, frontend, or observability files were edited.
