# v0.2.75 complexity-router default enablement boundary

- Raw decision: `docs/workingon/decision_v0.2.75_complexity_router_enablement_boundary.json`
- Decision: `require_live_validation_before_default_change`
- Default enabled: `False`
- Allowed to enable default: `True`
- Next version: `v0.2.76_complexity_router_live_validation_plan`
- First design: `docs/current-design/design_complexity_router_live_validation_plan.md`

| Option | Score | Disposition |
| --- | ---: | --- |
| `require_live_validation_before_default_change` | 12 | selected |
| `defer_enablement_indefinitely` | 8 | rejected because it loses the value of completed guardrails without adding evidence |
| `enter_enablement_review_now` | 7 | deferred until live validation plan exists |

## Conclusion

Default-safety prerequisites are satisfied, but default behavior remains disabled. Require a live validation plan before any default change.
