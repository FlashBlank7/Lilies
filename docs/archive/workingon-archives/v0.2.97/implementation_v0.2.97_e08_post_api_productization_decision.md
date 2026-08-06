# v0.2.97 E08 post-API productization decision implementation

## Summary

v0.2.97 selected the next E08 productization path after v0.2.96 completed the editable policy-controls backend API.

Selected path: `studio_editable_policy_controls`.

Next version: `v0.2.98_e08_studio_editable_policy_controls`.

## What changed

- Added deterministic selector: `scripts/v02_97_e08_post_api_productization_decision.py`.
- Added selector tests: `tests/test_v02_97_e08_post_api_productization_decision.py`.
- Generated decision evidence under `docs/workingon/`.
- Updated E08 ledger and v0.2 experiment status.

## Non-winning dispositions

- `operator_runbook_lifecycle`: deferred until an operator surface exists.
- `broader_sidecar_boundary_closure`: deferred because it is too broad for the immediate post-API slice.
- `pause_e08_after_api`: rejected because the API is not yet operator-accessible.

## Verification

- `.venv/bin/python scripts/v02_97_e08_post_api_productization_decision.py` -> `select_studio_editable_policy_controls`
- `.venv/bin/python -m pytest tests/test_v02_97_e08_post_api_productization_decision.py -q` -> `3 passed`

## E07 invariant

No E07 code, defaults, tests, frontend, or observability files were edited.
