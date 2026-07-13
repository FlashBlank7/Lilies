# v0.3.39 implementation archive - Try run active status refresh

## Source

- Source stage report: `docs/stage-reports/v0.3.38_try_run_duplicate_start_guard.md`
- Version: `v0.3.39_try_run_active_status_refresh`

## Customer Behavior Simulated

- A user starts a Try run and sees it queued.
- A user waits while the Try run is running.
- A non-technical user thinks the active status may be stale and needs to know whether to wait, cancel, or start another run.

## Implemented Changes

- Added `tryRunActiveStatus` derived from the current run status.
- Added `data-try-run-active-status` to the active-run guard.
- Added queued/running/fallback status labels in Chinese and English.
- Added auto-refresh expectation copy.
- Added stale-feeling guidance that says to wait or cancel explicitly.
- Styled the active status line inside the guard.
- Added v0.3.39 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 214.
- Repaired the v0.3.38 harness so it checks the stable current-gate id instead of the old exact pass count.

## Safety Boundary

- Display/copy only; no polling interval change.
- No new API endpoints.
- Live evidence called only `GET /health`.
- Duplicate-start guard remains active for queued/running.
- Explicit cancel remains the only stop action.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_39_try_run_active_status_refresh.py tests/test_v03_38_try_run_duplicate_start_guard.py -q`
- `76 passed`: `.venv/bin/python -m pytest tests/test_v03_39_try_run_active_status_refresh.py tests/test_v03_38_try_run_duplicate_start_guard.py tests/test_v03_37_try_run_mode_persistence.py tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py tests/test_v03_34_try_result_recovery_affordance.py tests/test_v03_33_try_run_result_interpretation.py tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `214 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.39/try_run_active_status_refresh_v0.3.39.json`
