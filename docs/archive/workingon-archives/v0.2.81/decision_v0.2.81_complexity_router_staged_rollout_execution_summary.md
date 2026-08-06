# v0.2.81 complexity-router staged rollout execution decision

- Raw decision: `docs/workingon-archives/v0.2.81/decision_v0.2.81_complexity_router_staged_rollout_execution.json`
- Decision: `execute_shadow_only_rollout`
- First stage: `stage_0_shadow_only`
- Default enabled: `False`
- Allowed to enable default: `True`
- Next version: `v0.2.82_complexity_router_shadow_only_rollout`
- First design: `docs/current-design/design_complexity_router_shadow_only_rollout.md`

| Option | Score | Disposition |
| --- | ---: | --- |
| `execute_shadow_only_rollout` | 15 | selected |
| `prepare_more_rollout_docs` | 9 | rejected because v0.2.80 already defines stages, controls, and rollback criteria |
| `defer_rollout_execution` | 8 | rejected because shadow-only execution has no behavior-change risk and produces evidence |

## Conclusion

Execute the shadow-only rollout next. Defaults remain disabled.
