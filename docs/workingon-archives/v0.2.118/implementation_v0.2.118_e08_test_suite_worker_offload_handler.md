# v0.2.118 E08 test_suite worker offload handler implementation

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.117_e08_remaining_sidecar_slice_reselection.md`
- Source task: `Implement test_suite worker offload handler`
- Version: `v0.2.118_e08_test_suite_worker_offload_handler`

## Implemented

- Added `test_suite_handler()` to `platform/backend/src/agent_platform/worker_runner.py`.
- Moved `test_suite` from deterministic unavailable catalog entry to implemented catalog entry.
- Registered the handler in `build_platform_worker_handlers()`.
- Extended `WorkflowRuntime.run_test_suite()` with optional `harness_task_id`, `manage_harness_task`, and `origin` parameters.
- Preserved existing API-created test-suite behavior.
- Preserved v0.2.112 heartbeat registry evidence through worker execution.

## Verification

- `.venv/bin/python -m pytest tests/test_v02_118_e08_test_suite_worker_offload_handler.py -q`
- `.venv/bin/python -m pytest tests/test_v02_110_e08_complete_handler_catalog.py tests/test_v02_114_e08_scheduler_trigger_worker_offload_handler.py tests/test_v02_116_e08_workflow_run_worker_offload_handler.py -q`
- `.venv/bin/python scripts/v02_118_e08_test_suite_worker_offload_handler.py`

## Boundary

This version closes only the `test_suite` worker offload handler slice.

Still not claimed:

- worker-owned handlers for `builder_build`, `benchmark`, and `draft_patch_preview`;
- production worker supervision;
- distributed queue backend;
- full Platform Harness sidecar completion.
