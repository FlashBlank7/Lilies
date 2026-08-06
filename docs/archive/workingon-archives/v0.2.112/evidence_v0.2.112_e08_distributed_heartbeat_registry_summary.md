# v0.2.112 E08 distributed heartbeat registry

- Raw evidence: `docs/workingon-archives/v0.2.112/evidence_v0.2.112_e08_distributed_heartbeat_registry.json`
- Status: `completed`
- Distributed queue implemented: `False`
- E08 full sidecar completion claimed: `False`
- Next boundary: This closes durable worker heartbeat/liveness registry only; distributed queue semantics, process supervision, external alerting, real worker-offload handlers, and external KMS remain open.

## Checks

| Check | Result |
| --- | --- |
| `heartbeat_persisted_across_harness_instances` | `True` |
| `active_liveness_exposed` | `True` |
| `stale_liveness_exposed` | `True` |
| `runner_sets_running_active_task` | `True` |
| `runner_returns_to_idle_with_last_task_metadata` | `True` |
| `worker_runner_task_succeeded` | `True` |

## Heartbeats

| Worker | Status | Liveness | Active task |
| --- | --- | --- | --- |
| `worker-runner` | `idle` | `active` | `` |
| `worker-stale` | `idle` | `stale` | `` |
| `worker-active` | `idle` | `active` | `` |

## Completed Slices Preserved

- `docs/workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md`
- `docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md`
- `docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md`
- `docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`
- `docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`
- `docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

## Implementation Paths

- `platform/backend/src/agent_platform/storage.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/worker_runner.py`
- `platform/backend/src/agent_platform/api.py`
- `tests/test_v02_112_e08_distributed_heartbeat_registry.py`
