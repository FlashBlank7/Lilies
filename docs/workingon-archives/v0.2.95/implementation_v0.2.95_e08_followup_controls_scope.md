# v0.2.95 E08 follow-up controls scope implementation

## Summary

v0.2.95 completed the scope decision requested by `docs/stage-reports/v0.2.94_productization_lane_reselection.md`.

The selected next implementation slice is `editable_policy_controls_api`, with next version `v0.2.96_e08_editable_policy_controls_api`.

## What changed

- Added `scripts/v02_95_e08_followup_controls_scope.py`.
- Added `tests/test_v02_95_e08_followup_controls_scope.py`.
- Generated `docs/workingon/scope_v0.2.95_e08_followup_controls.json`.
- Generated `docs/workingon/scope_v0.2.95_e08_followup_controls_summary.md`.
- Updated `docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md`.
- Updated `docs/experiment-status/v0.2_experiment_status.md`.

## Decision

The selector intentionally does not reselect already-closed E08 slices:

- cancellation/budget behavior evidence: already covered by v0.2.68;
- worker lease behavior: already covered by v0.2.20-v0.2.28 and backend tests;
- read-only policy-controls: already covered by v0.2.65-v0.2.66.

The product gap that remains actionable is an audited backend mutation contract for editable policy controls. Studio editable controls should follow after the backend contract exists.

## Verification

- `.venv/bin/python scripts/v02_95_e08_followup_controls_scope.py` -> `select_editable_policy_controls_api`
- `.venv/bin/python -m pytest tests/test_v02_95_e08_followup_controls_scope.py -q` -> `3 passed`

## E07 invariant

No E07 guarded default code, defaults, or tests were changed in this version.
