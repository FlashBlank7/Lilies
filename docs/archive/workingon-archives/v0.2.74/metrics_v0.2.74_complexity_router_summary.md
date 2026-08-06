# v0.2.74 complexity-router rollout metrics prerequisites

- Raw evidence: `docs/workingon/metrics_v0.2.74_complexity_router.json`
- Metrics prerequisite satisfied: `True`
- Metrics status: `ready_empty_state`
- Default enabled: `False`
- Allowed to enable default: `True`
- Missing prerequisites: none

| Metric | Description |
| --- | --- |
| `classification_distribution` | Count simple / medium / complex / unknown decisions over the rollout window. |
| `override_rate` | Share of classified requirements with an operator override. |
| `override_reason_coverage` | Share of force overrides with a non-empty operator-visible reason. |
| `fallback_unknown_rate` | Share of requirements classified as unknown and handled as complex-equivalent. |
| `success_rate_by_class` | Completion or acceptance rate grouped by effective requirement class. |
| `cost_latency_by_class` | Cost and latency distribution grouped by effective requirement class. |

## Conclusion

Rollout metrics prerequisites are satisfied as an API-visible empty-state schema. Default enablement remains off and requires a separate stage-report-selected decision.
