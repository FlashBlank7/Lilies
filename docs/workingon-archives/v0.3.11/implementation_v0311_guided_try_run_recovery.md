# implementation_v0311_guided_try_run_recovery

Version: `v0.3.11`
Stage: `guided_try_run_recovery`
Source stage report: `docs/stage-reports/v0.3.10_hydrated_frontend_verification_recovery.md`

## Work Performed

- Added a Try tab pre-run readiness panel for draft, inputs, published version, and latest run state.
- Added sample input fill action backed by first mandatory acceptance inputs/defaults.
- Added run recovery hints for missing input, failed, paused, and succeeded states.
- Added permission pause guidance in the permission card.
- Added stable `data-try-guidance` and `data-run-status` markers.
- Added `scripts/v03_11_guided_try_run_recovery.py` and `tests/test_v03_11_guided_try_run_recovery.py`.

## Verification

| Check | Result |
| --- | --- |
| Focused v0.3.11 and v0.3.10 tests | pass, `10 passed` |
| Live v0.3.11 guided Try run evidence | pass |
| Combined v0.3.x regression and stage template tests | pass, `56 passed` |
| Diff whitespace check | pass |

## Live Evidence Summary

- Evidence file: `docs/workingon/guided_try_run_recovery_v0.3.11.json`
- Smoke app: created and cleaned.
- Safe draft: Start to Answer skeleton seeded, revision `4`.
- Operator persona run: succeeded.
- Run output: `{"answer": "Summarize this failed order and name the next owner."}`
- Cleanup counts: `workflow_runs=1`, `builds=0`, `draft_idempotency=4`.
- Endpoint ledger: app create, four draft ops, run create, run polling, smoke cleanup.
- Forbidden build endpoint: not called.

## Known Limitations

- The live evidence verifies backend run behavior and source markers; full hydrated browser interaction remains unavailable.
- TypeScript/npm verification remains blocked by missing Node/npm, covered by v0.3.10 fallback checks.

## Outcome

v0.3.11 makes Try tab more usable for non-technical operators and proves a no-build safe draft run end to end.
