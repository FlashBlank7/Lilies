# implementation_v0.2.72_complexity_router_requirement_classification_contract

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.71_complexity_router_default_safety_gate.md`
- Source stage task: `Implement complexity-router requirement classification contract`; `Preserve operator override plan and rollout metrics prerequisites`; `Restore executable frontend verification when Node is available`
- Current design: `docs/current-design/design_complexity_router_requirement_classification_contract.md`; `docs/current-design/design_v0_2_72_override_metrics_preservation.md`; `docs/current-design/design_v0_2_72_frontend_verification_blocker.md`

## Changes

- Added deterministic requirement classification contract for `simple`, `medium`, `complex`, and `unknown`.
- Added conservative unknown handling: `unknown` is effective `complex`.
- Added authenticated API surfaces for classification contract status and one-off requirement classification.
- Updated default-safety status so `requirement_classification_contract` is satisfied.
- Preserved `operator_override_plan` and `rollout_metrics_prerequisites` as the remaining missing default-safety prerequisites.
- Retried frontend verification and recorded the unchanged Node/npm blocker.

## Evidence / Intermediate Results

Generated evidence:

- `docs/workingon/classification_v0.2.72_complexity_router.json`
- `docs/workingon/classification_v0.2.72_complexity_router_summary.md`

Decision:

- Contract satisfied: `True`
- Default enabled: `False`
- Allowed to enable default: `False`
- Missing prerequisites: `operator_override_plan`, `rollout_metrics_prerequisites`

## Verification

```text
.venv/bin/python -m pytest tests/test_complexity_router_default_safety.py
```

Result:

```text
6 passed, 1 warning in 0.33s
```

Focused regression:

```text
.venv/bin/python -m pytest tests/test_complexity_router_default_safety.py tests/test_workflow.py::test_platform_harness_policy_controls_api_reports_stdio_mcp_decisions tests/test_stage_report_template_validation.py
```

Result:

```text
9 passed, 1 warning in 0.32s
```

Evidence generation:

```text
.venv/bin/python scripts/v02_72_complexity_router_requirement_classification_contract.py
```

Result:

```text
docs/workingon/classification_v0.2.72_complexity_router.json
docs/workingon/classification_v0.2.72_complexity_router_summary.md
False
```

Frontend verification retry:

```text
zsh:1: command not found: npm
env: node: No such file or directory
```

## Remaining Risk

- Operator override plan and rollout metrics remain missing prerequisites.
- Complexity-router defaults remain disabled.
- Frontend executable verification remains blocked.

## Design Decision

- Continue current design / revise current design / proceed to next design / blocked: proceed to archive
