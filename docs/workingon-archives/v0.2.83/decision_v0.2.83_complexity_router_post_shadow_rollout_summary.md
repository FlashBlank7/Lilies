# v0.2.83 complexity-router post-shadow rollout decision

- Raw decision: `docs/workingon-archives/v0.2.83/decision_v0.2.83_complexity_router_post_shadow_rollout.json`
- Decision: `execute_operator_opt_in_rollout`
- Stage 0 passed: `True`
- Next stage: `stage_1_operator_opt_in`
- Next stage behavior change: `False`
- Default enabled: `False`
- Allowed to enable default: `True`
- Next version: `v0.2.84_complexity_router_operator_opt_in_rollout`
- First design: `docs/current-design/design_complexity_router_operator_opt_in_rollout.md`

| Option | Score | Disposition |
| --- | ---: | --- |
| `execute_operator_opt_in_rollout` | 15 | selected |
| `continue_shadow_only_observation` | 11 | rejected because stage_0 exit criteria are already satisfied and productization needs operator opt-in evidence |
| `begin_default_enablement_review` | 4 | rejected because operator opt-in evidence and frontend verification are still missing |

## Conclusion

Execute operator opt-in rollout next. Defaults remain disabled.
