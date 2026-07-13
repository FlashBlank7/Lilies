# implementation_v039_build_action_cost_guard

Version: `v0.3.9`
Stage: `build_action_cost_guard`
Source stage report: `docs/stage-reports/v0.3.8_runtime_connection_status_surface.md`

## Work Performed

- Added a two-step build intent guard to the home primary build action.
- Added a two-step build intent guard to the detail-page Start Team action.
- Kept the existing safe draft path as the direct no-build option.
- Added bilingual copy for guarded build actions and low-risk draft flow.
- Added stable `data-build-action` and `data-build-intent` markers for source and rendered checks.
- Added visible guard styling for default and armed states.
- Added `scripts/v03_9_build_action_guard.py` and `tests/test_v03_9_build_action_guard.py`.

## Verification

| Check | Result |
| --- | --- |
| Focused v0.3.9 tests | pass, `4 passed` |
| Live v0.3.9 no-build smoke | pass |
| Combined v0.3.x regression and stage template tests | pass, `46 passed` |
| Diff whitespace check | pass |

## Live Evidence Summary

- Evidence file: `docs/workingon/build_action_guard_v0.3.9.json`
- Runtime health: pass, backend `v0.3.6`, current code ready.
- Rendered home guard marker: pass.
- Rendered detail guard marker: pass.
- Smoke app cleanup: pass.
- Endpoint ledger: `POST /api/v1/applications`, `POST /api/v1/applications/{id}/smoke-cleanup`.
- Forbidden build endpoint: not called.

## Known Limitations

- Client-side hydrated click behavior is still inferred from React source and rendered initial markers; Browser runtime is unavailable in this environment.
- TypeScript/npm verification is still blocked because `node` and `npm` are not on this shell PATH.

## Outcome

v0.3.9 closes the accidental build-start risk for exploratory users by making model/team work a deliberate second action while preserving safe draft as the low-risk path.
