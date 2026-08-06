# v0.3.45 implementation archive

## Scope

Added an explicit recovered-ready state for Try inputs after a parser error clears.

## Product Change

- Tracked whether a Try input parser error had appeared during the current editing session.
- Showed a recovered-ready section only after that error clears and at least one input field exists.
- Marked run controls with `data-try-input-recovery-ready`.
- Added a payload-preview focus action so the next step is review, not an implicit run.
- Cleared the recovered-ready state after an explicit successful run start.

## Files

- `platform/frontend/app/applications/[id]/page.tsx`
- `platform/frontend/lib/i18n.ts`
- `platform/frontend/app/globals.css`
- `scripts/v03_45_try_run_input_error_recovery_ready_state.py`
- `tests/test_v03_45_try_run_input_error_recovery_ready_state.py`
- `docs/testing/regression_lanes.json`

## Verification

- `tests/test_v03_45_try_run_input_error_recovery_ready_state.py`: `6 passed`
- `tests/test_v03_44_try_run_input_error_action_guard.py`: `6 passed`
- Current v0.3.x release gate: `250 passed, 1 warning`
- Live evidence: `docs/workingon-archives/v0.3.45/try_run_input_error_recovery_ready_state_v0.3.45.json`

