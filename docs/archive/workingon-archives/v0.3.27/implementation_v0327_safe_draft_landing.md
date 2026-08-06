# v0.3.27 implementation summary

## Source

- Source stage report: `docs/stage-report-archives/v0.3.x/v0.3.26_recommended_create_action_affordance.md`
- Source tasks:
  - Start `v0.3.27_safe_draft_landing_handoff`
  - Add safe-draft landing banner
  - Add safe-draft next-step actions
  - Extend safe-draft landing harness
  - Preserve regression lane manifest

## Implemented

- Added `safeDraftLanding` URL state in the detail page.
- Added a safe-draft landing banner in the canvas guidance area.
- Added next-step buttons for inspect, acceptance, try, and build later.
- Kept all handoff buttons as `type="button"` and tab-only actions.
- Added bilingual landing copy and responsive styling.
- Added v0.3.27 deterministic harness, bug ledger, and read-only live evidence.
- Added focused tests and extended the v0.3.x current release gate to 142 expected passes.

## Verification

- Focused v0.3.27/v0.3.26/v0.3.25/v0.3.24/v0.3.10 tests: `30 passed`.
- Live evidence: `.venv/bin/python scripts/v03_27_safe_draft_landing.py --live --api-url http://127.0.0.1:8001`.
- Current v0.3.x release gate: `142 passed, 1 warning`.
- Safety boundary: live evidence calls only `GET /health`; safe-draft actions route only to tabs and do not call build/test/run/draft mutation endpoints.

## Residual Risk

- Browser and TypeScript verification remain unavailable because the shell PATH still has no `node` or `npm`.
- The safe-draft banner currently persists while `safeDraft=1` remains in the URL; a later stage should add a dismiss/acknowledge action that clears only this query flag.
