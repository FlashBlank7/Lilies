# v0.3.42 implementation archive - Try run status last updated

## Source

- Source stage report: `docs/stage-report-archives/v0.3.x/v0.3.41_try_run_cancel_progress_feedback.md`
- Version: `v0.3.42_try_run_status_last_updated`

## Customer Behavior Simulated

- A user waits on an active run and wants to know whether the page is still checking.
- A user sees cancelling/cancelled status and needs to distinguish local refresh recency from backend completion time.
- A user reads the result card after a poll tick.

## Implemented Changes

- Added `formatRunStatusCheckedAt`.
- Added `runStatusCheckedAt` local state.
- Set last-checked time when a new run is queued.
- Updated last-checked time on every run polling tick.
- Updated last-checked time when a cancel request is confirmed.
- Added a result-card recency strip with `data-try-status-recency="last-checked"`.
- Added bilingual recency copy that keeps current status/output as the source of truth.
- Added v0.3.42 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 232.
- Repaired the v0.3.41 harness so it checks the stable current-gate id instead of the old exact pass count.

## Safety Boundary

- Frontend local timestamp only; no backend API change.
- No polling interval change.
- Live evidence called only `GET /health`.
- Live evidence forbids run/build/test/version/draft/cancel mutation endpoints.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_42_try_run_status_last_updated.py tests/test_v03_41_try_run_cancel_progress_feedback.py -q`
- `94 passed`: `.venv/bin/python -m pytest tests/test_v03_42_try_run_status_last_updated.py tests/test_v03_41_try_run_cancel_progress_feedback.py tests/test_v03_40_try_run_cancel_safety_confirmation.py tests/test_v03_39_try_run_active_status_refresh.py tests/test_v03_38_try_run_duplicate_start_guard.py tests/test_v03_37_try_run_mode_persistence.py tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py tests/test_v03_34_try_result_recovery_affordance.py tests/test_v03_33_try_run_result_interpretation.py tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `232 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.42/try_run_status_last_updated_v0.3.42.json`
