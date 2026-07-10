# v0.2.91 complexity-router runtime activation observability

- Raw evidence: `docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability.json`
- Status: `completed`
- Default metrics active count: `0`
- Default metrics disabled-default count: `1`
- Enabled metrics active count: `2`
- Enabled metrics bypassed count: `1`
- Enabled metrics conservative-unknown count: `1`
- Enabled metrics request-override count: `1`
- Enabled planning-mode distribution: `{'auto': 1, 'disabled': 2}`
- Enabled reuse-depth distribution: `{'none': 1, 'adaptive': 1, 'shallow': 1}`
- Frontend verification passed: `True`

## Metrics Contract

- Metrics distinguish active, bypassed, disabled-default, conservative-unknown, and request-override decisions.
- Metrics expose classification, effective planning mode, runtime reuse depth, build outcome, and sampled records.
- Metrics are read-only and preserve rollback value `disabled`.
