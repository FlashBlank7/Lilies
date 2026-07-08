# work_platform_harness_owner_budget_2026_07_09

## Goal

推进 `v0.2.11_platform_harness_owner_budget`：在 v0.2.10 的 durable task usage 之上，为 Platform Harness 增加 owner/account 级累计预算硬边界。

## Scope

包含：

- 增加 owner-level usage aggregation。
- 增加可配置 owner-level model/tool/node budget。
- 在 `PlatformHarness.record_usage()` 中检查 owner-level budget。
- 增加跨 task owner budget regression test。
- 完成 stage archive 和 design recycling。

不包含：

- 计费账户模型。
- 时间窗口预算重置。
- UI 展示。
- secret policy。
- network egress policy。
- stale running task reconciliation。

## Linked Current Design

- `docs/current-design/design_platform_harness_owner_budget_v1.md`

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Audit v0.2.10 design archive gate | completed |
| 2 | Add owner usage aggregation in Storage | completed |
| 3 | Add owner budget config and enforcement in PlatformHarness | completed |
| 4 | Add cross-task owner budget regression test | completed |
| 5 | Run focused and full verification | completed |
| 6 | Archive v0.2.11 with design recycling | completed |

## Acceptance Criteria

- Owner-level budgets are disabled by default to preserve current behavior.
- When enabled, cumulative usage across multiple tasks for the same `owner_id` is blocked.
- The violating task is marked `failed` and records a clear owner-budget error.
- Existing task-level budget tests still pass.

## Current Decision

Proceed to archive `v0.2.11`. Time-windowed budgets and real billing account mapping are deferred until the project has an account model.

## Implementation Evidence

- Config added:
  - `platform_harness_max_model_calls_per_owner`
  - `platform_harness_max_tool_calls_per_owner`
  - `platform_harness_max_node_executions_per_owner`
- Storage added:
  - `sum_platform_usage_count(owner_id, usage_type)`
- Platform Harness added:
  - owner-level violation checks in `record_usage()`
  - persisted failed task status when owner budget is exceeded
- Regression test added:
  - `test_platform_harness_owner_budget_blocks_cross_task_usage`

Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_tasks_persist_across_app_instances tests/test_workflow.py::test_platform_harness_owner_budget_blocks_cross_task_usage tests/test_workflow.py::test_platform_harness_node_budget_blocks_run -q
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
- `59 passed, 1 warning`

Paid/live model test:

- Not required. This stage is deterministic Platform Harness enforcement and does not change model/provider behavior.
