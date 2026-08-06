# v0.2.106 E08 stdio/container egress allowlist contract

- Raw evidence: `docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract.json`
- Status: `completed`
- E08 full sidecar completion claimed: `False`
- Next boundary: This closes the stdio/container egress allowlist contract slice only; KMS/rotation, complete handler catalog, distributed heartbeat registry, and other sidecar slices remain open.

## Checks

| Check | Result |
| --- | --- |
| `covered_sandboxed_stdio_allowlist_allowed` | `True` |
| `missing_declared_hosts_blocked` | `True` |
| `platform_unlisted_host_blocked` | `True` |
| `unsandboxed_allowlist_blocked` | `True` |
| `blocked_before_external_action` | `True` |
| `policy_controls_allowlist_supported` | `True` |

## Implementation Paths

- `platform/backend/src/agent_platform/models.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/runtime.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `tests/test_v02_106_e08_stdio_container_egress_allowlist_contract.py`
- `tests/test_runtime.py`
- `tests/test_workflow.py`
