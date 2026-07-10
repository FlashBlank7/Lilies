# E08 control-behavior matrix v0.2.66

- Raw matrix: `docs/workingon-archives/v0.2.66/e08_control_behavior_matrix_v0.2.66.json`
- Current slice: `e08_policy_controls_surface`
- Not full sidecar completion: `True`

| Control | Layer | Enforcement | Status | Source |
| --- | --- | --- | --- | --- |
| `workflow_passmode` | workflow_internal | soft_configurable | available | `docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md` |
| `cancellation_checkpoint` | workflow_runtime | soft_checkpoint | available | `platform/backend/src/agent_platform/workflow_runtime.py` |
| `budget_limits` | platform_harness | hard_counter | configured | `platform/backend/src/agent_platform/platform_harness.py` |
| `worker_lease` | platform_harness | lease_coordination | enabled | `platform/backend/src/agent_platform/platform_harness.py` |
| `network_egress_policy` | platform_harness | hard_boundary | restricted | `platform/backend/src/agent_platform/platform_harness.py` |
| `secret_policy` | platform_harness | hard_boundary | enabled | `platform/backend/src/agent_platform/platform_harness.py` |
