# v0.4.3 Integrated Browser Closure

Status: active

## Contract

- Task: `V04-03-T01F`
- Intents: `PRODUCT-003`, `PRODUCT-004`, `PRODUCT-005`, `PRODUCT-006`, `PRODUCT-007`

## Problem

Component tests can all pass while a customer still sees a confusing or broken sequence. This task proves the vertical from application creation through mode selection, evidence, repair, configuration, and publication at the actual UI boundary.

## Boundary

- Test a deterministic local fixture for the normal path; use a bounded real-model check only where repair quality depends on model behavior and a configured key is available.
- Browser absence or provider absence narrows the claim; it cannot be written as a pass.
- Desktop and mobile checks focus on the customer-critical path, no overlap, no crash, and accurate policy messaging.

## Design

- Add one API integration scenario covering legacy migration, each delivery mode, tested edit -> stale evidence, warning acknowledgement, governed denial, repair apply, and version record.
- Start backend and frontend locally, then use Playwright against a clean dedicated application.
- Capture desktop and mobile screenshots for mode choice, stale evidence, failed test repair, block form, and publish decision.
- Assert browser console has no uncaught errors and node click/config interaction does not crash.
- Run frontend lint/build, v0.4.x gate, process-control suite, and full suite with classification report.
- Request a fresh read-only closure audit only after all five preceding tasks are complete.

## Acceptance

- The complete non-technical journey is understandable and functional in both viewports.
- Policy copy matches backend decisions.
- No frontend crash or incoherent overlap appears.
- The final report distinguishes deterministic, browser, model, and unavailable evidence levels.

## Implemented So Far

- One public-API integration scenario migrates a legacy application and exercises all three delivery modes, current-to-stale evidence, acknowledgement publication, governed denial, real failed-case repair, and immutable publication decisions.
- Invalid-draft acceptance no longer returns an empty test list. It returns case-level preflight failures without fabricating a run, allowing the real UI report to enter repair preview.
- Worker completion now reports the persisted terminal state: an expired lease cannot produce a succeeded return value, and the full-suite-discovered timing regression remains preserved as a classified failure artifact.
- A deterministic fixture script creates isolated stale/configuration, failed-repair, and governed-publication applications for repeatable browser checks.
- A machine-readable journey fixes six required interactions across 1440x900 desktop and 390x844 mobile viewports, with seven named screenshots.
- `scripts/v04_03_browser_closure_gate.py` rejects closure unless every interaction passes in both viewports, screenshot PNGs exist under versioned evidence with matching SHA-256, the browser console is observed cleanly, and overlap inspection covers both viewports.
- `scripts/v04_03_browser_evidence_recorder.py` records browser selection, per-viewport interactions, screenshots, console state, and overlap checks through atomic JSON replacement. Partial updates always retain `blocked`; only `finalize` can produce `passed`, and it delegates to the closure validator.
- `scripts/v04_03_browser_environment.py` prepares the isolated Next standalone assets, starts the current backend and frontend on dedicated ports, waits for real HTTP health, supervises both processes, and cleans up both children on interruption or failure. Its `check` command distinguishes a ready product target from Browser-provider availability.
- The frontend passes current TypeScript and isolated Webpack production-build checks, the current release gate passes 54 tests, process controls pass 30 tests, and the full suite passes 740 tests with exactly 17 strict historical xfails.
- Runtime health on the dedicated current-code instance reports `v0.4.3`.
- The supervised isolated frontend fixture at `http://127.0.0.1:3001` and backend health endpoint at `http://127.0.0.1:8002/health` both return HTTP 200; a concrete application route also returns HTTP 200. The environment runs from ignored build/data paths and stops cleanly without interfering with the user's existing development services.

## Evidence Ceiling

- Browser runtime setup succeeded, but URL selection returned `No browser is available` and discovery returned an empty list.
- Repeated retries against the supervised standalone target produced the same Browser-provider result. Revision 2 classifies this as `blocked_by_environment` evidence debt, forbids another retry without a recheck trigger, and does not treat it as a campaign blocker.
- Desktop/mobile screenshots, overlap inspection, interaction assertions, and browser-console checks were not run and are not claimed.
- Evidence: `docs/workingon/v0.4.3_browser_fixture.json` and `docs/workingon/v0.4.3_browser_verification.json`.
- The closure gate currently exits non-zero with all missing Browser observations enumerated; this is expected blocker evidence, not a failed implementation test.
- Component/integration implementation is complete. Browser-level usability, crash, console, screenshot, and overlap claims remain unavailable and capped until a supported provider completes the prepared journey.
