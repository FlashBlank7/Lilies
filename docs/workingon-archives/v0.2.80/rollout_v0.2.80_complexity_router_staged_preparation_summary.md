# v0.2.80 complexity-router staged rollout preparation

- Raw evidence: `docs/workingon/rollout_v0.2.80_complexity_router_staged_preparation.json`
- Default enabled: `False`
- Allowed to enable default: `True`
- Stage count: `3`

| Stage | Mode | Behavior change |
| --- | --- | --- |
| `stage_0_shadow_only` | `shadow_only` | `False` |
| `stage_1_operator_opt_in` | `operator_opt_in` | `False` |
| `stage_2_limited_default_review` | `limited_default_review_ready` | `False` |

## Rollback Criteria

`unexpected_classification_rate_above_0.05`, `missing_required_metrics`, `override_reason_coverage_below_0.95`, `any_accidental_default_enablement`

## Conclusion

Staged rollout preparation is defined. Defaults remain disabled.
