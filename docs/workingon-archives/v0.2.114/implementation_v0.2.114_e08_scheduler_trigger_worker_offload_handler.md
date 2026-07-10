# v0.2.114 E08 scheduler_trigger worker offload handler implementation

## Source

- Source stage report: `docs/stage-reports/v0.2.113_e08_remaining_sidecar_slice_reselection.md`
- Source task: `Implement scheduler_trigger worker offload handler`
- Version: `v0.2.114_e08_scheduler_trigger_worker_offload_handler`

## Implemented

- Added `scheduler_trigger_handler()` to `platform/backend/src/agent_platform/worker_runner.py`.
- Moved `scheduler_trigger` from deterministic unavailable catalog entry to implemented catalog entry.
- Added scheduler support for executing an already-claimed schedule fire through shared scheduler/runtime behavior.
- Added optional `scheduler_worker_offload_enabled` mode; default direct scheduler behavior remains available.
- In offload mode, `WorkflowScheduler.tick()` claims the schedule fire, queues a `scheduler_trigger` Platform Harness task, and leaves execution to the worker.
- Worker execution preserves `scheduler_fire` usage records and v0.2.112 heartbeat registry evidence.

## Verification

- `.venv/bin/python -m pytest tests/test_v02_114_e08_scheduler_trigger_worker_offload_handler.py -q`
- `.venv/bin/python -m pytest tests/test_v02_110_e08_complete_handler_catalog.py -q`
- `.venv/bin/python -m pytest tests/test_workflow.py -q -k 'daily_schedule or worker_scheduler_manual_trigger or worker_runner'`
- `.venv/bin/python scripts/v02_114_e08_scheduler_trigger_worker_offload_handler.py`

## Boundary

This version closes only the `scheduler_trigger` worker offload handler slice.

Still not claimed:

- production worker supervision;
- distributed queue backend;
- worker-owned handlers for `workflow_run`, `builder_build`, `test_suite`, `benchmark`, and `draft_patch_preview`;
- full Platform Harness sidecar completion.
