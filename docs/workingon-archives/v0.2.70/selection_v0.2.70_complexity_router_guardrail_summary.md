# v0.2.70 complexity-router guardrail selection

- Raw selection: `docs/workingon-archives/v0.2.70/selection_v0.2.70_complexity_router_guardrail.json`
- Winner: `default_safety_gate`
- Router ready for default: `False`
- Next version: `v0.2.71_complexity_router_default_safety_gate`
- First design: `docs/current-design/design_complexity_router_default_safety_gate.md`

| Candidate | Score | Disposition |
| --- | ---: | --- |
| `default_safety_gate` | 19 | selected |
| `requirement_classification_contract` | 14 | deferred as supporting input after default-safety gate contract |
| `rollout_metrics` | 12 | deferred until the default-safety and classification contracts define measurable states |
| `override_controls` | 11 | deferred because editable/operator controls should follow the default-safety contract |

## Conclusion

Select the default-safety gate first. E07 remains not ready for default routing; classification, override controls, and rollout metrics are deferred supporting guardrails.
