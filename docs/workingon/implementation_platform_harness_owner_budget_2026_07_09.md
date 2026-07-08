# implementation_platform_harness_owner_budget_2026_07_09

## Summary

`v0.2.11` implemented owner-level Platform Harness budget enforcement. The feature is disabled by default and becomes active when an owner-level budget config is set above `0`.

## Code Changes

- `platform/backend/src/agent_platform/config.py`
  - Added owner-level budget fields for model calls, tool calls, and node executions.
- `platform/backend/src/agent_platform/api.py`
  - Passed owner-level budget settings into `PlatformHarness`.
- `platform/backend/src/agent_platform/storage.py`
  - Added `sum_platform_usage_count(owner_id, usage_type)`.
- `platform/backend/src/agent_platform/platform_harness.py`
  - Added owner-level budget checks before usage mutation.
  - Marks the violating task failed and persists the failure.
- `tests/test_workflow.py`
  - Added `test_platform_harness_owner_budget_blocks_cross_task_usage`.

## Verification

Focused:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_tasks_persist_across_app_instances tests/test_workflow.py::test_platform_harness_owner_budget_blocks_cross_task_usage tests/test_workflow.py::test_platform_harness_node_budget_blocks_run -q
```

Result:

- `3 passed, 1 warning`

Full:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py
.venv/bin/python -m ruff check platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/storage.py platform/backend/src/agent_platform/platform_harness.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `59 passed, 1 warning`

## Paid/Live Test Decision

Skipped intentionally. The feature is deterministic enforcement and does not affect Builder Team generation, model provider compatibility, or benchmark semantics.

## Remaining Risk

- The budget is cumulative over durable task history and has no reset window.
- `owner_id` is not yet a formal billing account.
- UI visibility for owner-level budget status is not implemented.

