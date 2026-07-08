# implementation_platform_harness_durable_storage_2026_07_09

## Summary

`v0.2.10` implemented durable Platform Harness task records. The Platform Harness still uses an in-process cache for active mutation, but SQLite is now the durable source for task monitor records exposed by the existing API.

## Code Changes

- `platform/backend/src/agent_platform/storage.py`
  - Added `platform_harness_tasks`.
  - Added save/get/list/count methods for platform task records.
  - Stores full `record_json` plus indexed fields for query filters.
- `platform/backend/src/agent_platform/platform_harness.py`
  - Persists `PlatformTaskRecord` after start, usage, violation, and finish transitions.
  - Hydrates task records from storage when a new app instance queries them.
  - Counts active tasks from durable storage for the existing active-task limit.
- `tests/test_workflow.py`
  - Added `test_platform_harness_tasks_persist_across_app_instances`.

## Verification

Focused:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_tracks_test_suite_and_workflow_usage tests/test_workflow.py::test_platform_harness_node_budget_blocks_run tests/test_workflow.py::test_builder_benchmark_suite_reports_aggregate_trends_and_harness_usage tests/test_workflow.py::test_platform_harness_tasks_persist_across_app_instances -q
```

Result:

- `4 passed, 1 warning`

Full:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py
.venv/bin/python -m ruff check platform/backend/src/agent_platform/storage.py platform/backend/src/agent_platform/platform_harness.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `58 passed, 1 warning`

## Paid/Live Test Decision

Skipped intentionally. This stage is deterministic storage and retrieval work. It does not change Builder Team prompts, model calls, workflow generation, or provider compatibility.

## Remaining Risk

- A crashed process can leave tasks marked `running`; the active-task limit will treat them as active until a future recovery policy closes or reconciles them.
- Task monitor records are durable, but execution itself is still not a durable queue.
- Account-level budget, secret policy, and network egress policy remain future Platform Harness stages.

