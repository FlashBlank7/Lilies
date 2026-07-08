# design_platform_harness_stale_task_reconciliation_v1

## 1. Goal

Add stale active task reconciliation to Platform Harness so durable `queued` / `running` records left by a crashed process do not permanently consume active-task slots.

## 2. Module Boundary

In scope:

- `platform/backend/src/agent_platform/config.py`
- `platform/backend/src/agent_platform/storage.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/api.py`
- `tests/test_workflow.py`

Out of scope:

- Durable execution resume.
- Worker lease ownership.
- Human review UI for stale records.
- Secret or egress policy.

## 3. Control Flow

```text
PlatformHarness.start_task(new_task)
  -> PlatformHarness.reconcile_stale_tasks()
  -> Storage.fail_stale_platform_tasks(cutoff)
  -> update old active task record_json/status/error/finished_at
  -> emit platform_harness.task.failed events for reconciled tasks
  -> count active tasks
  -> allow or reject new task
```

## 4. Implementation Plan

1. Add config:
   - `platform_harness_stale_active_task_seconds`
2. Default to `0.0`, meaning disabled.
3. Add `Storage.fail_stale_platform_tasks(cutoff, error)`.
4. Add `PlatformHarness.reconcile_stale_tasks()`.
5. Call reconciliation at the start of `start_task()` before active count.
6. Add regression test:
   - set `max_active_tasks=1`
   - create task A and leave it `running`
   - wait past stale threshold
   - start task B
   - assert task A is `failed` and task B is `running`

## 5. Referenced Intellectual Assets

- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`

## 6. Risks

- A too-small stale threshold can fail legitimately long-running tasks.
- Without worker leases, this is a coarse recovery policy rather than full durable execution.
- Reconciliation emits failure events after the fact; it cannot reconstruct the missing process-side stack trace.

## 7. Acceptance Criteria

- Default config does not change existing active task behavior.
- Stale active tasks are marked failed durably.
- Active task limit no longer counts reconciled stale tasks.
- Tests cover the active-slot recovery path.

## 8. Implementation Result

Status: implemented.

Implemented modules:

- `platform/backend/src/agent_platform/config.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/storage.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `tests/test_workflow.py`

Verification:

- Focused Platform Harness stale reconciliation tests passed: `3 passed, 1 warning`.
- Full backend pytest passed: `60 passed, 1 warning`.
- Changed-file ruff passed.
- Compile check passed.

Boundary:

- Reconciliation is disabled by default with config value `0.0`.
- Stale tasks are terminalized as `failed`, not resumed.
- Full durable execution and worker lease ownership remain future work.

