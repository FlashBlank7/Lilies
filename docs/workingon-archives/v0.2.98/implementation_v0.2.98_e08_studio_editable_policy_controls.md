# v0.2.98 E08 Studio editable policy-controls implementation

## Summary

v0.2.98 implemented the Studio editable policy-controls slice selected by `docs/stage-reports/v0.2.97_e08_post_api_productization_decision.md`.

## What changed

- Added frontend PATCH request/response types in `platform/frontend/lib/platform.ts`.
- Added editable policy-controls form to the Studio monitor tab in `platform/frontend/app/applications/[id]/page.tsx`.
- Added controls for network policy, allowlist, cancellation policy, secret policy, worker lease seconds, budget limits, and required reason.
- Added save action wired to `PATCH /api/v1/platform/harness/policy-controls`.
- Added compact form styling in `platform/frontend/app/globals.css`.
- Added i18n labels in `platform/frontend/lib/i18n.ts`.
- Added v0.2.98 evidence script and test.

## Verification

- `PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint` in `platform/frontend` -> passed.
- Next dev route smoke: `curl -I http://127.0.0.1:3108/applications/smoke-v02-98` -> `200`.
- `.venv/bin/python -m pytest tests/test_v02_98_e08_studio_editable_policy_controls.py tests/test_v02_97_e08_post_api_productization_decision.py -q` -> `4 passed`.
- `.venv/bin/python -m pytest tests/test_workflow.py -k 'policy_controls' tests/test_v02_96_e08_editable_policy_controls_api.py -q` -> `4 passed, 71 deselected, 1 warning`.
- `.venv/bin/python scripts/v02_98_e08_studio_editable_policy_controls.py` -> `studio_editable_policy_controls_evidence`.

## Boundary

This is a Studio UI slice, not full Platform Harness sidecar completion. Operator runbook and broader sidecar boundary closure remain future stage-report choices.

## E07 invariant

No E07 complexity-router code, defaults, tests, frontend, or observability files were edited.
