# v0.2.85 implementation evidence

## Scope

- Source stage report: `docs/stage-reports/v0.2.84_complexity_router_operator_opt_in_rollout.md`
- Source task: `Decide post-operator-opt-in rollout path`
- Version: `v0.2.85_complexity_router_post_operator_opt_in_decision`

## Implemented

- Added `scripts/v02_85_complexity_router_post_operator_opt_in_decision.py`.
- Added `tests/test_v02_85_complexity_router_post_operator_opt_in_decision.py`.
- Generated decision evidence:
  - `docs/workingon-archives/v0.2.85/decision_v0.2.85_complexity_router_post_operator_opt_in.json`
  - `docs/workingon-archives/v0.2.85/decision_v0.2.85_complexity_router_post_operator_opt_in_summary.md`
- Updated E07 experiment ledger and v0.2 experiment status.

## Decision

- Selected option: `repair_frontend_verification_environment`
- Rejected option: `continue_operator_opt_in_observation`
- Rejected option: `begin_default_enablement_review`
- Next version: `v0.2.86_frontend_verification_environment_repair`
- First design: `docs/current-design/design_frontend_verification_environment_repair.md`

## Frontend environment probe

- `package_json_present=true`
- `package_lock_present=true`
- `node_modules_present=true`
- `node=false`
- `npm=false`
- `executable_frontend_verification_available=false`

## Default safety

- `default_enabled=false`
- `allowed_to_enable_default=true`
- No runtime default was changed in this version.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| v0.2.85 decision tests and related rollout/safety tests | passed | `.venv/bin/python -m pytest tests/test_v02_85_complexity_router_post_operator_opt_in_decision.py tests/test_v02_84_complexity_router_operator_opt_in_rollout.py tests/test_complexity_router_default_safety.py` -> `14 passed, 1 warning` |
| Post-operator-opt-in decision evidence generation | passed | `.venv/bin/python scripts/v02_85_complexity_router_post_operator_opt_in_decision.py` -> `repair_frontend_verification_environment` |
| Frontend lint retry | blocked | `npm run lint || true` -> `zsh:1: command not found: npm` |
| Frontend TypeScript retry | blocked | `node_modules/.bin/tsc --noEmit || true` -> `env: node: No such file or directory` |

## Next

The next-stage task must be carried by the v0.2.85 stage report, not this workingon evidence file.
