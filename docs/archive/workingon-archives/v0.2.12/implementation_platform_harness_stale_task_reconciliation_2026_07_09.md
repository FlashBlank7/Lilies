# implementation_platform_harness_stale_task_reconciliation_2026_07_09

## Summary

`v0.2.12` implemented stale active task reconciliation for Platform Harness. When configured, a new `start_task()` call marks old `queued` / `running` task records as `failed` before enforcing the active task limit.

## Code Changes

- `platform/backend/src/agent_platform/config.py`
  - Added `platform_harness_stale_active_task_seconds`.
- `platform/backend/src/agent_platform/api.py`
  - Passed stale threshold into `PlatformHarness`.
- `platform/backend/src/agent_platform/storage.py`
  - Added `fail_stale_platform_tasks(cutoff, error)`.
  - Updates indexed task fields and full `record_json`.
- `platform/backend/src/agent_platform/platform_harness.py`
  - Added `reconcile_stale_tasks()`.
  - Calls reconciliation before active task count in `start_task()`.
  - Emits `platform_harness.task.failed` events for reconciled tasks.
- `tests/test_workflow.py`
  - Added `test_platform_harness_reconciles_stale_active_tasks`.

## Verification

Focused:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_reconciles_stale_active_tasks tests/test_workflow.py::test_platform_harness_owner_budget_blocks_cross_task_usage tests/test_workflow.py::test_platform_harness_tasks_persist_across_app_instances -q
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
- `60 passed, 1 warning`

## Paid/Live Test Decision

Skipped intentionally. The feature is deterministic recovery policy and does not affect Builder Team generation, model provider compatibility, or benchmark semantics.

## Remaining Risk

- A too-low stale threshold can fail legitimately long-running tasks.
- Stale reconciliation does not resume work; it only terminalizes abandoned monitor records.
- Worker leases and durable execution remain future work.

