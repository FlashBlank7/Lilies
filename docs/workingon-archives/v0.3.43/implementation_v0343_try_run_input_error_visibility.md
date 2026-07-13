# v0.3.43 implementation archive - Try run input error visibility

## Source

- Source stage report: `docs/stage-reports/v0.3.42_try_run_status_last_updated.md`
- Version: `v0.3.43_try_run_input_error_visibility`

## Customer Behavior Simulated

- A user leaves a required field empty.
- A user types a non-number into a numeric field.
- A user enters invalid JSON into object/array input.
- A user needs to return to the input form without triggering a run API call.

## Implemented Changes

- Added `tryInputErrorVisible` derived from `runInputParsed.error`.
- Added an inline `try-input-error` panel near the JSON payload preview.
- Reused the current parser error text in the inline panel.
- Added correction guidance that points to the input form and payload preview.
- Added a local focus action for the run input form.
- Added v0.3.43 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 238.
- Repaired the v0.3.42 harness so it checks the stable current-gate id instead of the old exact pass count.

## Safety Boundary

- Frontend visibility and focus only; no backend API change.
- Existing `startRun(...)` parser guard remains the final submission boundary.
- Live evidence called only `GET /health`.
- Live evidence forbids run/build/test/version/draft/cancel mutation endpoints.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_43_try_run_input_error_visibility.py tests/test_v03_42_try_run_status_last_updated.py -q`
- `100 passed`: `.venv/bin/python -m pytest tests/test_v03_43_try_run_input_error_visibility.py tests/test_v03_42_try_run_status_last_updated.py tests/test_v03_41_try_run_cancel_progress_feedback.py tests/test_v03_40_try_run_cancel_safety_confirmation.py tests/test_v03_39_try_run_active_status_refresh.py tests/test_v03_38_try_run_duplicate_start_guard.py tests/test_v03_37_try_run_mode_persistence.py tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py tests/test_v03_34_try_result_recovery_affordance.py tests/test_v03_33_try_run_result_interpretation.py tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `238 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.43/try_run_input_error_visibility_v0.3.43.json`
