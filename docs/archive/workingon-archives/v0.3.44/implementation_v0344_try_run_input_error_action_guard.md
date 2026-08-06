# v0.3.44 implementation archive - Try run input error action guard

## Source

- Source stage report: `docs/stage-report-archives/v0.3.x/v0.3.43_try_run_input_error_visibility.md`
- Version: `v0.3.44_try_run_input_error_action_guard`

## Customer Behavior Simulated

- A user sees an inline parser error and repeatedly clicks Try draft.
- A user sees an inline parser error and tries published run.
- A user needs to understand why run actions are disabled and return to the error.

## Implemented Changes

- Added `tryInputErrorRef`.
- Added `tryInputErrorBlockingRun`.
- Added testable `data-try-input-error-action-guard`.
- Disabled draft and published run actions while parser errors exist.
- Kept active-run duplicate-start guard in the disabled expressions.
- Added an input action guard strip with local focus behavior.
- Updated `startRun(...)` parser-error early return to focus the inline error.
- Added bilingual guard copy and styling.
- Added v0.3.44 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 244.
- Repaired v0.3.38/v0.3.39 harness marker fragility after the disabled expressions gained the input-error guard.

## Safety Boundary

- Frontend guard and local focus only; no backend API change.
- Existing parser validation remains in `startRun(...)`.
- Live evidence called only `GET /health`.
- Live evidence forbids run/build/test/version/draft/cancel mutation endpoints.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_44_try_run_input_error_action_guard.py tests/test_v03_43_try_run_input_error_visibility.py -q`
- `106 passed`: `.venv/bin/python -m pytest tests/test_v03_44_try_run_input_error_action_guard.py tests/test_v03_43_try_run_input_error_visibility.py tests/test_v03_42_try_run_status_last_updated.py tests/test_v03_41_try_run_cancel_progress_feedback.py tests/test_v03_40_try_run_cancel_safety_confirmation.py tests/test_v03_39_try_run_active_status_refresh.py tests/test_v03_38_try_run_duplicate_start_guard.py tests/test_v03_37_try_run_mode_persistence.py tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py tests/test_v03_34_try_result_recovery_affordance.py tests/test_v03_33_try_run_result_interpretation.py tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `244 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.44/try_run_input_error_action_guard_v0.3.44.json`
