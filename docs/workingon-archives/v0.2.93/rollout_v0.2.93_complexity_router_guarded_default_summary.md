# v0.2.93 complexity-router guarded default rollout

- Raw evidence: `docs/workingon-archives/v0.2.93/rollout_v0.2.93_complexity_router_guarded_default.json`
- Status: `completed`
- Default mode: `limited_default`
- Default limited enabled: `True`
- Default safety enabled: `True`
- Default plan enabled: `True`
- Simple default active: `True`
- Simple runtime reuse depth: `shallow`
- Unknown default active: `False`
- Request override source: `request_override`
- Rollback plan enabled: `False`
- Rollback build active: `False`
- Frontend verification passed: `True`

## Rollout Boundary

- Normal settings now use guarded limited-default routing.
- Explicit `disabled` settings remain the rollback path.
- Unknown requirements remain bypassed and complex-equivalent.
