# v0.2.69 E08 continuation decision

- Raw decision: `docs/workingon-archives/v0.2.69/decision_v0.2.69_e08_continuation.json`
- Decision: `pause_e08_move_complexity_router`
- Next version: `v0.2.70_complexity_router_guardrail_selection`
- First design: `docs/current-design/design_complexity_router_guardrail_selection.md`

| Option | Score | Disposition |
| --- | ---: | --- |
| `pause_e08_move_complexity_router` | 18 | selected |
| `continue_e08_operator_runbook` | 10 | deferred because it is lower value after current backend evidence and before editable controls |
| `continue_e08_editable_policy_controls` | 7 | deferred because Node/frontend verification is still blocked and read-only evidence is sufficient for now |
| `declare_full_sidecar_complete` | -3 | rejected because v0.2.65-v0.2.68 explicitly do not close full sidecar completion |

## Conclusion

Pause the current E08 productization tranche after surface, matrix, and cancellation/budget evidence. Move next to complexity-router guardrail selection while keeping E08 editable controls and runbook deferred.
