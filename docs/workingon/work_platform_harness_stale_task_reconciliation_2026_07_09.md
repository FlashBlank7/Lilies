# work_platform_harness_stale_task_reconciliation_2026_07_09

## Goal

推进 `v0.2.12_platform_harness_stale_task_reconciliation`：在 v0.2.10 durable task storage 和 v0.2.11 owner budget 之后，增加 stale active task reconciliation，避免崩溃遗留的 `queued` / `running` task 永久占用 active-task slots。

## Scope

包含：

- 增加可配置 stale active task 阈值。
- 在 `start_task()` 计算 active task limit 前执行 stale reconciliation。
- 把过旧 active tasks 标记为 `failed`，写入 error 和 metadata。
- 增加回归测试：stale task 被终止后，新 task 可以启动。

不包含：

- durable execution resume。
- worker queue recovery。
- UI 展示。
- 人工确认 stale task 的操作界面。

## Linked Current Design

- `docs/current-design/design_platform_harness_stale_task_reconciliation_v1.md`

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Audit v0.2.11 design archive gate | completed |
| 2 | Add stale task config and storage reconciliation method | completed |
| 3 | Wire reconciliation into PlatformHarness start path | completed |
| 4 | Add active-slot regression test | completed |
| 5 | Run focused and full verification | completed |
| 6 | Archive v0.2.12 with design recycling | completed |

## Acceptance Criteria

- Reconciliation is disabled by default.
- When enabled, stale `queued` / `running` task records become `failed`.
- Starting a new task after reconciliation no longer fails the active-task limit because of stale records.
- The stale failure is durable and queryable.

## Current Decision

Proceed to archive `v0.2.12`. Conservative automatic terminalization is implemented: stale active tasks become `failed`, not `cancelled`, because the process may have crashed and cannot confirm user intent.

## Implementation Evidence

- Config added:
  - `platform_harness_stale_active_task_seconds`
- Storage added:
  - `fail_stale_platform_tasks(cutoff, error)`
- Platform Harness added:
  - `reconcile_stale_tasks()`
  - start-path reconciliation before active task count
- Regression test added:
  - `test_platform_harness_reconciles_stale_active_tasks`

Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_reconciles_stale_active_tasks tests/test_workflow.py::test_platform_harness_owner_budget_blocks_cross_task_usage tests/test_workflow.py::test_platform_harness_tasks_persist_across_app_instances -q
```

Result:

- `3 passed, 1 warning`

Full verification:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py
.venv/bin/python -m ruff check platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/storage.py platform/backend/src/agent_platform/platform_harness.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `60 passed, 1 warning`

Paid/live model test:

- Not required. This stage is deterministic Platform Harness recovery policy and does not change model/provider behavior.
