# work_v0.2.52_adaptive_default_productization

## Goal

Turn the `v0.2.51` adaptive defaultization gate into real product behavior: adaptive becomes the default Builder/API suggestion mode, while explicit fixed-depth overrides remain available and observable.

## Source

- Stage report: `docs/stage-report-archives/v0.2.x/v0.2.51_e05_second_family_adaptive_validation.md`
- Version: `v0.2.52`

## Full Task Set

| Task | Disposition | Design | Reason |
| --- | --- | --- | --- |
| Productize adaptive as the default Builder/API suggestion mode | accepted | `design_adaptive_default_api_and_builder.md` | The gate is now passed, so the runtime default should stop pretending shallow is the baseline. |
| Add adaptive rollout observability and rollback semantics | accepted | `design_adaptive_default_observability_and_override.md` | A default switch must stay visible and reversible, not hidden inside one fallback branch. |
| Keep E08 sidecar/passmode as a separate lane | deferred | none | Independent Harness experiment track. |

## Execution Status

| Design | Status | Evidence | Next action |
| --- | --- | --- | --- |
| `design_adaptive_default_api_and_builder.md` | completed | `platform/backend/src/agent_platform/template_strategy.py`; `platform/backend/src/agent_platform/api.py`; `platform/backend/src/agent_platform/builder.py`; `tests/test_workflow.py`; `docs/workingon-archives/v0.2.52/implementation_v0.2.52_adaptive_default_productization.md` | proceed to archive |
| `design_adaptive_default_observability_and_override.md` | completed | `platform/backend/src/agent_platform/template_strategy.py`; `platform/backend/src/agent_platform/api.py`; `platform/backend/src/agent_platform/builder.py`; `tests/test_workflow.py`; `docs/workingon-archives/v0.2.52/implementation_v0.2.52_adaptive_default_productization.md` | proceed to archive |

## Acceptance

- All tasks dispositioned: yes
- All accepted designs completed/blocked/deferred: yes
- Verification: focused backend regression passed
- Experiment status updated: yes
- Archive ready: yes
