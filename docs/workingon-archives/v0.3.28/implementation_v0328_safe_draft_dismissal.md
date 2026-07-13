# v0.3.28 implementation summary

## Source

- Source stage report: `docs/stage-reports/v0.3.27_safe_draft_landing_handoff.md`
- Source tasks:
  - Start `v0.3.28_safe_draft_landing_dismissal`
  - Add safe-draft landing dismiss action
  - Clear only safeDraft URL state
  - Extend dismissal harness
  - Preserve regression lane manifest

## Implemented

- Added `dismissSafeDraftLanding` to hide the safe-draft landing banner.
- Added a dismiss action in the safe-draft handoff action row.
- Deleted only the `safeDraft` query parameter with `URLSearchParams`.
- Preserved remaining URL state such as `tab`.
- Used `window.history.replaceState` so dismissal does not reload or navigate.
- Added bilingual dismiss copy and styling.
- Added v0.3.28 deterministic harness, bug ledger, and read-only live evidence.
- Added focused tests and extended the v0.3.x current release gate to 148 expected passes.

## Verification

- Focused v0.3.28/v0.3.27/v0.3.26/v0.3.25/v0.3.10 tests: `30 passed`.
- Live evidence: `.venv/bin/python scripts/v03_28_safe_draft_dismissal.py --live --api-url http://127.0.0.1:8001`.
- Current v0.3.x release gate: `148 passed, 1 warning`.
- Safety boundary: live evidence calls only `GET /health`; dismiss uses local state and URL replacement only.

## Residual Risk

- Browser and TypeScript verification remain unavailable because the shell PATH still has no `node` or `npm`.
- The detail build tab still lacks the create page's local requirement-readiness hints, which matters after users choose "build later".
