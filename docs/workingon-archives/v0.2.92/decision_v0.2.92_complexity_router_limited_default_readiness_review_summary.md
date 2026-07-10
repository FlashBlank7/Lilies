# v0.2.92 complexity-router limited default readiness review

- Raw evidence: `docs/workingon-archives/v0.2.92/decision_v0.2.92_complexity_router_limited_default_readiness_review.json`
- Status: `completed`
- Decision: `enter_guarded_default_rollout`
- Next version: `v0.2.93_complexity_router_guarded_default_rollout`
- Normal default settings: `disabled`
- Gates passed: `7/7`
- Frontend verification passed: `True`

## Gate Results

- `runtime_activation_evidence`: `True` - explicit limited-default metrics must include at least two active runtime decisions
- `observability_categories`: `True` - metrics must distinguish active, bypassed, unknown, and request override decisions
- `disabled_default_safety`: `True` - normal default settings must remain inactive
- `unknown_bypass_safety`: `True` - unknown requirements must remain bypassed and counted
- `request_override_visibility`: `True` - request override visibility must be present before broader rollout
- `rollback_to_disabled`: `True` - rollback value must remain disabled
- `frontend_verification`: `True` - frontend verification must pass

## Decision Boundary

- This readiness review does not change normal default settings.
- A guarded default rollout must preserve rollback value `disabled` and conservative unknown bypass behavior.
