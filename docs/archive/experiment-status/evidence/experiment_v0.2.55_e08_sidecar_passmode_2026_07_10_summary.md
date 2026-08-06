# E08 harness sidecar/passmode comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10.json`
- Status: `completed`
- Paid/live model required: `False`

## Scenarios

| Scenario | Layer | Passmode | Status | Enforcement | Bypassable | Recovery |
| --- | --- | --- | --- | --- | --- | --- |
| workflow_internal_permission_pause | workflow_internal_soft_harness | always_ask | paused | soft_pause | True | resume_with_human_approval_or_preset_input |
| workflow_internal_permission_auto_approve | workflow_internal_soft_harness | auto_approve | succeeded | soft_pass | True | not_needed_when_passmode_allows |
| platform_sidecar_network_block | platform_harness_sidecar | platform_policy_none | failed | hard_block | False | change_platform_policy_or_remove_external_action_then_retry |

## Conclusion

workflow-internal passmode can pause or pass by workflow configuration; Platform Harness sidecar policy is a hard boundary that fails the run before the external action.
