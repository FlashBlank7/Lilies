# v0.2.98 E08 Studio editable policy-controls evidence

- Raw evidence: `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls.json`
- Status: `completed`
- Studio page: `platform/frontend/app/applications/[id]/page.tsx`
- Type contract: `platform/frontend/lib/platform.ts`

## Checks

- `patch_endpoint_wired`: `True`
- `save_function_present`: `True`
- `editable_controls_present`: `True`
- `type_contract_present`: `True`
- `i18n_present`: `True`
- `css_present`: `True`

## Verification

- Frontend lint: `PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint`
- Route smoke: `curl -I http://127.0.0.1:3108/applications/smoke-v02-98 -> 200`
- Backend regression: `.venv/bin/python -m pytest tests/test_workflow.py -k 'policy_controls' tests/test_v02_96_e08_editable_policy_controls_api.py -q`
- E07 invariant: `preserved`
- Not full sidecar completion: `True`
