# v0.2.90 implementation evidence: complexity-router runtime activation path

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.89_complexity_router_limited_default_enablement_contract.md`
- Source tasks:
  - Implement complexity-router runtime activation path
  - Preserve rollback-to-disabled and conservative unknown handling
  - Maintain executable frontend verification evidence

## Completed

- Added `runtime_activation_for_build()` to classify build requirements and derive a runtime builder policy only when limited-default settings explicitly enable it.
- Persisted `complexity_router` activation evidence and `runtime_builder_policy` in `BuildTeamState`.
- Wired `POST /api/v1/applications/{application_id}/builds` through the runtime activation path.
- Mapped router `plan_first` into effective build planning mode when the request uses `planning_mode="auto"`.
- Made Builder `template_suggestions` use runtime builder policy `reuse_depth` when the Builder omits an explicit reuse depth and no build-plan override exists.
- Preserved disabled/default behavior and conservative unknown handling.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| v0.2.90 runtime activation tests | `4 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_v02_90_complexity_router_runtime_activation_path.py` |
| v0.2.90 plus prior contract/safety and Builder planning/template checks | `20 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_v02_90_complexity_router_runtime_activation_path.py tests/test_v02_89_complexity_router_limited_default_enablement_contract.py tests/test_complexity_router_default_safety.py tests/test_workflow.py::test_builder_template_suggestions_default_to_adaptive_when_omitted tests/test_workflow.py::test_builder_planning_mode_required_blocks_mutation_before_plan tests/test_workflow.py::test_builder_planning_mode_disabled_rejects_build_plan_tool` |
| Runtime activation evidence generation | `completed` | `.venv/bin/python scripts/v02_90_complexity_router_runtime_activation_path.py` |
| Default settings behavior | `active=false`; no runtime builder policy | `docs/workingon-archives/v0.2.90/activation_v0.2.90_complexity_router_runtime_activation_path_summary.md` |
| Explicit simple limited-default behavior | `active=true`; effective planning mode `disabled`; runtime reuse depth `shallow` | `docs/workingon-archives/v0.2.90/activation_v0.2.90_complexity_router_runtime_activation_path_summary.md` |
| Builder omitted template suggestion behavior | `reuse_depth=shallow`; `reuse_depth_source=complexity_router` | `docs/workingon-archives/v0.2.90/activation_v0.2.90_complexity_router_runtime_activation_path_summary.md` |
| Unknown behavior | `active=false`; no runtime builder policy | `docs/workingon-archives/v0.2.90/activation_v0.2.90_complexity_router_runtime_activation_path_summary.md` |
| Frontend verification | `passed=true` | `docs/workingon-archives/v0.2.90/activation_v0.2.90_complexity_router_runtime_activation_path.json` |

## Product Boundary

v0.2.90 makes explicit limited-default routing affect real build runtime behavior. It does not change normal default settings: default settings still keep runtime routing inactive.
