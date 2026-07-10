# v0.2.89 implementation evidence: complexity-router limited default enablement contract

## Source

- Source stage report: `docs/stage-reports/v0.2.88_complexity_router_limited_default_enablement_plan.md`
- Source tasks:
  - Implement complexity-router limited default enablement contract
  - Preserve default-disabled status
  - Maintain executable frontend verification evidence

## Completed

- Added backend settings for `complexity_router_default_mode`, `complexity_router_limited_default_enabled`, and `complexity_router_limited_default_min_confidence`.
- Extended requirement classification output with configured default mode, limited-default eligibility, default router status, and config-gated `default_builder_policy`.
- Added `/api/v1/platform/complexity-router/default-enableable-plan` to expose the current limited-default contract, rollback value, operator controls, rollback triggers, and default safety gate.
- Preserved normal settings as `disabled`; default classification keeps `default_router_enabled=false`.
- Kept unknown requirements conservative: unknown input remains complex-equivalent and not default-router-enabled even under explicit limited-default settings.
- Regenerated contract evidence and frontend verification evidence.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| v0.2.89 contract tests plus prior safety/plan tests | `15 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_v02_89_complexity_router_limited_default_enablement_contract.py tests/test_complexity_router_default_safety.py tests/test_v02_88_complexity_router_limited_default_enablement_plan.py` |
| Contract evidence generation | `completed` | `.venv/bin/python scripts/v02_89_complexity_router_limited_default_enablement_contract.py` |
| Default settings status | `default_enabled=false` | `docs/workingon-archives/v0.2.89/contract_v0.2.89_complexity_router_limited_default_enablement_summary.md` |
| Explicit limited-default status | `default_enabled=true` when explicitly configured | `docs/workingon-archives/v0.2.89/contract_v0.2.89_complexity_router_limited_default_enablement_summary.md` |
| Unknown handling | `default_router_enabled=false` | `docs/workingon-archives/v0.2.89/contract_v0.2.89_complexity_router_limited_default_enablement_summary.md` |
| Frontend verification | `passed=true` | `docs/workingon-archives/v0.2.89/contract_v0.2.89_complexity_router_limited_default_enablement.json` |

## Product Boundary

v0.2.89 implements the config/API/classification contract and proves that explicit limited-default mode can surface a default builder policy. It does not make limited default the normal runtime default; default settings remain disabled.
