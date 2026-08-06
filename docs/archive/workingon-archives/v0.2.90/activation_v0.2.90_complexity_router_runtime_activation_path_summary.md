# v0.2.90 complexity-router runtime activation path

- Raw evidence: `docs/workingon-archives/v0.2.90/activation_v0.2.90_complexity_router_runtime_activation_path.json`
- Status: `completed`
- Default settings active: `False`
- Explicit simple active: `True`
- Explicit simple effective planning mode: `disabled`
- Explicit simple runtime reuse depth: `shallow`
- Omitted template suggestion reuse depth: `shallow`
- Omitted template suggestion source: `complexity_router`
- Unknown active: `False`
- Frontend verification passed: `True`

## Runtime Contract

- Default settings do not activate runtime builder policy.
- Explicit limited-default settings can activate simple routing and set `planning_mode=disabled`.
- Builder `template_suggestions` without an explicit reuse depth uses runtime policy `shallow`.
- Unknown requirements remain inactive and do not persist runtime builder policy.
