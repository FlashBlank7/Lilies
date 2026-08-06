# v0.2.96 E08 editable policy-controls API evidence

- Raw evidence: `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api.json`
- Status: `completed`
- Endpoint: `PATCH /api/v1/platform/harness/policy-controls`
- Network policy before: `full`
- Network policy after: `allowlist`
- Cancellation policy after: `disabled`
- Worker lease after: `30.0`
- Changed fields: `network_egress_policy, network_egress_allowlist, cancellation_policy, secret_policy_enabled, worker_lease_seconds, limits.max_model_calls_per_task, limits.max_tool_calls_per_owner`
- Invalid update rejection status: `422`
- E07 invariant: `preserved`
- Not full sidecar completion: `True`
