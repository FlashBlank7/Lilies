# v0.2.116 E08 workflow_run worker offload handler implementation

## Source

- Source stage report: `docs/stage-reports/v0.2.115_e08_remaining_sidecar_slice_reselection.md`
- Source task: `Implement workflow_run worker offload handler`
- Version: `v0.2.116_e08_workflow_run_worker_offload_handler`

## Implemented

- Added `workflow_run_handler()` to `platform/backend/src/agent_platform/worker_runner.py`.
- Moved `workflow_run` from deterministic unavailable catalog entry to implemented catalog entry.
- Registered the handler in `build_platform_worker_handlers()`.
- Reused `WorkflowRuntime.create_run()` with `parent_task_id` set to the queued worker task id and `origin="worker"`.
- Preserved existing API-created workflow run behavior with `origin="api"`.
- Preserved v0.2.112 heartbeat registry evidence through worker execution.

## Verification

- `.venv/bin/python -m pytest tests/test_v02_116_e08_workflow_run_worker_offload_handler.py -q`
- `.venv/bin/python -m pytest tests/test_v02_110_e08_complete_handler_catalog.py tests/test_v02_114_e08_scheduler_trigger_worker_offload_handler.py -q`
- `.venv/bin/python scripts/v02_116_e08_workflow_run_worker_offload_handler.py`

## Boundary

This version closes only the `workflow_run` worker offload handler slice.

Still not claimed:

- worker-owned handlers for `builder_build`, `test_suite`, `benchmark`, and `draft_patch_preview`;
- production worker supervision;
- distributed queue backend;
- full Platform Harness sidecar completion.
