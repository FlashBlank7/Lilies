# v0.2.79 complexity-router default enablement review decision

- Raw decision: `docs/workingon/decision_v0.2.79_complexity_router_default_enablement_review.json`
- Decision: `prepare_staged_rollout`
- Live evidence: `completed` / passed `True`
- Provider/model: `deepseek` / `deepseek-v4-pro`
- Default enabled: `False`
- Allowed to enable default: `True`
- Next version: `v0.2.80_complexity_router_staged_rollout_preparation`
- First design: `docs/current-design/design_complexity_router_staged_rollout_preparation.md`

| Option | Score | Disposition |
| --- | ---: | --- |
| `prepare_staged_rollout` | 17 | selected |
| `continue_deferral` | 12 | rejected because it wastes completed guardrail and live evidence without adding a safer rollout path |
| `enter_immediate_enablement_review` | 11 | deferred until staged rollout preparation exists and frontend verification is restored or explicitly waived |

## Conclusion

Prepare staged rollout before any default enablement. This preserves the positive live evidence while avoiding an immediate default behavior change.
