# v0.3.37 implementation archive - Try run mode persistence

## Source

- Source stage report: `docs/stage-reports/v0.3.36_try_run_mode_visibility.md`
- Version: `v0.3.37_try_run_mode_persistence`

## Customer Behavior Simulated

- A user refreshes or remounts the detail page and still needs to understand whether the latest Try result was draft or published mode.
- A browser may contain invalid localStorage values that must not mislabel the result.

## Implemented Changes

- Added application-scoped `RUN_MODE_STORAGE_PREFIX`.
- Added `runModeStorageKey`, `readStoredRunMode`, `persistRunMode`, and `isRunMode`.
- Loaded stored mode on application id change.
- Persisted explicit draft/published mode in `startRun(useDraft)`.
- Added v0.3.37 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 202.

## Safety Boundary

- localStorage read/write only.
- Invalid, missing, empty, or `unknown` stored values fall back to `unknown`.
- Explicit new run mode overrides stored mode.
- Live evidence called only `GET /health`.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_37_try_run_mode_persistence.py tests/test_v03_36_try_run_mode_visibility.py -q`
- `64 passed`: `.venv/bin/python -m pytest tests/test_v03_37_try_run_mode_persistence.py tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py tests/test_v03_34_try_result_recovery_affordance.py tests/test_v03_33_try_run_result_interpretation.py tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `202 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.37/try_run_mode_persistence_v0.3.37.json`
