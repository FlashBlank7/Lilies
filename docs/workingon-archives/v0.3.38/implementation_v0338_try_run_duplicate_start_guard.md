# v0.3.38 implementation archive - Try run duplicate-start guard

## Source

- Source stage report: `docs/stage-report-archives/v0.3.x/v0.3.37_try_run_mode_persistence.md`
- Version: `v0.3.38_try_run_duplicate_start_guard`

## Customer Behavior Simulated

- A user clicks Try draft or published run again while the current run is queued or running.
- A user sees disabled run buttons and needs to know whether to wait or cancel.
- A user should still be able to retry after failed, paused, succeeded, or cancelled results.

## Implemented Changes

- Added `isActiveRunStatus`.
- Added `tryRunActive`.
- Added early return in `startRun(...)` for queued/running.
- Disabled draft and published run buttons during active runs.
- Added `data-try-run-start-guard="active"` guidance strip.
- Added bilingual guard copy and styling.
- Added v0.3.38 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 208.

## Safety Boundary

- Guard blocks only duplicate starts.
- Cancel remains explicit and available for queued/running active runs.
- Failed, paused, succeeded, cancelled, and no-run states do not block new explicit starts.
- Live evidence called only `GET /health`.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_38_try_run_duplicate_start_guard.py tests/test_v03_37_try_run_mode_persistence.py -q`
- `70 passed`: `.venv/bin/python -m pytest tests/test_v03_38_try_run_duplicate_start_guard.py tests/test_v03_37_try_run_mode_persistence.py tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py tests/test_v03_34_try_result_recovery_affordance.py tests/test_v03_33_try_run_result_interpretation.py tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `208 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.38/try_run_duplicate_start_guard_v0.3.38.json`
