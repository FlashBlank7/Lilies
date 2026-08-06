# v0.2.84 complexity-router operator opt-in rollout

- Raw evidence: `docs/workingon-archives/v0.2.84/rollout_v0.2.84_complexity_router_operator_opt_in.json`
- Status: `completed`
- Reason: stage_1 operator opt-in exit criteria satisfied
- Stage: `stage_1_operator_opt_in`
- Mode: `operator_opt_in`
- Behavior change: `False`
- Default enabled: `False`
- Allowed to enable default: `True`
- Sample count: `3`
- Override reason coverage: `1.0`
- Unexpected classification rate: `0.0`
- Accidental default enablement count: `0`

| Case | Expected | Predicted | Operator mode | Effective | Reason captured | Passed |
| --- | --- | --- | --- | --- | --- | --- |
| `simple_text_edit` | `simple` | `simple` | `force_simple` | `simple` | `True` | `True` |
| `medium_api_workflow` | `medium` | `medium` | `force_medium` | `medium` | `True` | `True` |
| `complex_platform_guardrail` | `complex` | `complex` | `force_complex` | `complex` | `True` | `True` |

## Metrics

- Classification distribution: `{"complex": 1, "medium": 1, "simple": 1}`
- Override rate: `1.0`
- Override reason coverage: `1.0`
- Unexpected classification rate: `0.0`

## Pass / Fail

override reason coverage and unexpected classification rate satisfied
