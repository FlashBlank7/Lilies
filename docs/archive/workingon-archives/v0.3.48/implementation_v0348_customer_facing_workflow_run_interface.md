# implementation_v0348_customer_facing_workflow_run_interface

## Summary

v0.3.48 creates the first customer-facing Run tab surface.

The Run tab now starts from a user mental model:

- what this workflow does,
- what the user needs to enter,
- how many steps the workflow has,
- whether data has started flowing,
- which step appears current from available run events,
- what result or error the user received.

Technical readiness, sample input, payload JSON, full run JSON, and trace remain available, but they are no longer the first thing a non-technical user must parse.

## Implementation

| Area | Change |
| --- | --- |
| Run overview | Added `data-customer-run-interface="overview"` with purpose, input count, step count, mode, status, and step preview. |
| Start controls | Added `data-customer-run-interface="start-controls"` around sample input, input form, validation, and explicit draft/published buttons. |
| Raw payload boundary | Moved payload preview into secondary `details` with `data-customer-run-interface="raw-payload"`. |
| Progress | Added `data-customer-run-interface="step-progress"` with data-flow cards and per-step status derived from existing run events. |
| Result | Added `data-customer-run-interface="result-card"` with customer-readable output/error preview before technical details. |
| i18n/style | Added zh/en copy and compact customer-run styling. |
| Release gate | Added `scripts/v03_48_customer_facing_workflow_run_interface.py`, `tests/test_v03_48_customer_facing_workflow_run_interface.py`, and updated pass count to `271`. |

## Verification

| Check | Result |
| --- | --- |
| v0.3.48 focused tests | `8 passed` |
| v0.3.48 evidence script | `passed` |
| Frontend TypeScript | `tsc --noEmit` passed |
| Current v0.3.x release gate | `271 passed, 1 warning` |
| Live `/health` evidence | `passed`; read-only `GET /health` only |
| Browser attempt | Browser connector unavailable; plugin troubleshooting file missing in cache |

## Evidence

- `docs/workingon-archives/v0.3.48/customer_facing_workflow_run_interface_v0.3.48.json`
- `tests/test_v03_48_customer_facing_workflow_run_interface.py`
- `scripts/v03_48_customer_facing_workflow_run_interface.py`

## Handoff

The next stage should validate and specialize the customer journey against a real scenario, especially the user's Japanese-student example: topic input, comment-expression collection workflow description, visible run steps, and final spoken-Japanese summary result.
