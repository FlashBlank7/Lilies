# v0.3.36 implementation archive - Try run mode visibility

## Source

- Source stage report: `docs/stage-reports/v0.3.35_try_result_output_preview.md`
- Version: `v0.3.36_try_run_mode_visibility`

## Customer Behavior Simulated

- A user sees a Try result and needs to know whether it came from current draft debugging or published-version validation.
- A user refreshes the page or sees older state and needs an explicit unknown fallback instead of a misleading label.

## Implemented Changes

- Added `RunMode`.
- Added local `lastRunMode` state.
- Set `lastRunMode` inside `startRun(useDraft)`.
- Added `data-try-run-mode` mode strip in the result panel.
- Added `data-try-run-mode-action` to draft and published run buttons.
- Added draft/published/unknown guidance copy and mode styling.
- Added v0.3.36 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 196.

## Safety Boundary

- No backend schema change.
- No new run mutation beyond existing explicit run buttons.
- Live evidence called only `GET /health`.
- Evidence records `model_call_used=false` and `forbidden_endpoint_called=false`.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py -q`
- `58 passed`: `.venv/bin/python -m pytest tests/test_v03_36_try_run_mode_visibility.py tests/test_v03_35_try_result_output_preview.py tests/test_v03_34_try_result_recovery_affordance.py tests/test_v03_33_try_run_result_interpretation.py tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `196 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.36/try_run_mode_visibility_v0.3.36.json`
