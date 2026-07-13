# v0.3.32 implementation archive - Try run sample input visibility

## Source

- Source stage report: `docs/stage-reports/v0.3.31_detail_build_recommended_action.md`
- Version: `v0.3.32_try_run_sample_input_visibility`

## Customer Behavior Simulated

- A non-technical user enters the Try tab after creating or building a draft.
- The user sees the `填入样例输入` button but does not know what values it will insert.
- The user has missing required input or invalid JSON and needs a next action that does not require reading implementation details.

## Implemented Changes

- Added a Try tab sample-input preview panel with field count, required count, acceptance-sample count, source labels, and compact value previews.
- Added a deterministic next-action strip with `no_inputs`, `fill_sample`, and `run_draft` states.
- Added `data-try-sample-input="summary"`, `data-try-sample-next-action`, and `data-try-sample-action="fill-sample"` markers for automation.
- Added bilingual copy and compact panel styling.
- Added v0.3.32 source, fixture, safety, bug ledger, live health, and manifest tests.
- Updated the current v0.3.x release gate expected pass count to 172.

## Safety Boundary

- No new run entry point was added.
- The sample-fill button only mutates local form state.
- Live evidence called only `GET /health`.
- Evidence records `model_call_used=false` and `forbidden_endpoint_called=false`.

## Verification

- `12 passed`: `.venv/bin/python -m pytest tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py -q`
- `30 passed`: `.venv/bin/python -m pytest tests/test_v03_32_try_run_sample_input.py tests/test_v03_31_detail_build_recommended_action.py tests/test_v03_30_detail_build_action.py tests/test_v03_29_detail_build_readiness.py tests/test_v03_10_frontend_verification_recovery.py -q`
- `172 passed, 1 warning`: current v0.3.x release gate.
- Live evidence: `docs/workingon-archives/v0.3.32/try_run_sample_input_v0.3.32.json`
