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

