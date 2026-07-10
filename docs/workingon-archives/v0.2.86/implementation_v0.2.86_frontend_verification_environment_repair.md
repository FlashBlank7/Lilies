# v0.2.86 implementation evidence

## Scope

- Source stage report: `docs/stage-reports/v0.2.85_complexity_router_post_operator_opt_in_decision.md`
- Source task: `Repair frontend verification environment`
- Version: `v0.2.86_frontend_verification_environment_repair`

## Implemented

- Added `scripts/frontend_verification_runner.py`.
- Added `scripts/v02_86_frontend_verification_environment_repair.py`.
- Added `tests/test_v02_86_frontend_verification_environment_repair.py`.
- Generated frontend verification repair evidence:
  - `docs/workingon-archives/v0.2.86/verification_v0.2.86_frontend_environment_repair.json`
  - `docs/workingon-archives/v0.2.86/verification_v0.2.86_frontend_environment_repair_summary.md`
- Updated E07 experiment ledger and v0.2 experiment status.

## Repair result

- Selected Node bin: `/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin`
- `node_available=true`
- `npm_available=true`
- `npm run lint`: return code `0`
- `node_modules/.bin/tsc --noEmit`: return code `0`

## Default safety

- `default_enabled=false`
- `allowed_to_enable_default=true`
- No runtime default was changed in this version.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| v0.2.86 repair tests and related decision/template tests | passed | `.venv/bin/python -m pytest tests/test_v02_86_frontend_verification_environment_repair.py tests/test_v02_85_complexity_router_post_operator_opt_in_decision.py tests/test_stage_report_template_validation.py` -> `6 passed` |
| Frontend verification repair evidence generation | passed | `.venv/bin/python scripts/v02_86_frontend_verification_environment_repair.py` -> `completed` |
| Frontend lint | passed | `npm run lint` -> return code `0` through repaired PATH |
| Frontend TypeScript | passed | `node_modules/.bin/tsc --noEmit` -> return code `0` through repaired PATH |

## Next

The next-stage task must be carried by the v0.2.86 stage report, not this workingon evidence file.
