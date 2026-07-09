# work_platform_harness_durable_storage_2026_07_09

## Goal

推进 `v0.2.10_platform_harness_durable_storage`：把 Platform Harness task records 从单进程内存扩展为 SQLite 持久化记录，使 task monitor boundary 在后端重启或重新创建 app instance 后仍可查询。

## Scope

包含：

- 新增 Platform Harness task record 持久化表和 Storage 方法。
- 让 `PlatformHarness.start_task()`、`record_usage()`、`finish_task()` 写入持久层。
- 让 `get_task()` 和 `list_tasks()` 可从持久层恢复历史记录。
- 增加跨 app instance 查询的回归测试。
- 更新 implementation evidence、stage report 和 historical design。

不包含：

- account-level budget。
- secret policy。
- network egress policy。
- durable queue、retry worker 或 Temporal 级 durable execution。
- 前端 UI 改版。

## Linked Current Design

- `docs/historical-designs/v0.2.10_design_platform_harness_durable_storage_v1.md`

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Read Platform Harness v1 design, Storage, API and tests | completed |
| 2 | Add durable task record storage schema and methods | completed |
| 3 | Persist PlatformHarness task lifecycle transitions | completed |
| 4 | Add cross-instance persistence regression test | completed |
| 5 | Run focused and full verification | completed |
| 6 | Archive v0.2.10 with design recycling | completed |

## Acceptance Criteria

- A task created by one app instance can be queried from a new app instance using the same `data_dir`.
- Existing `/api/v1/platform/harness/tasks` and `/tasks/{task_id}` response shape remains unchanged.
- Budget violation status, usage counts, metadata, and errors survive persistence.
- Focused Platform Harness tests pass.
- Full backend pytest and changed-file ruff pass.

## Implementation Evidence

- Storage schema added: `platform_harness_tasks`.
- Storage methods added: `save_platform_task()`, `get_platform_task()`, `list_platform_tasks()`, `count_platform_tasks()`.
- `PlatformHarness` now persists records after `start_task()`, `record_usage()`, and `finish_task()`.
- `PlatformHarness.get_task()` and `list_tasks()` hydrate from durable storage.
- Regression test added: `test_platform_harness_tasks_persist_across_app_instances`.

Verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_tracks_test_suite_and_workflow_usage tests/test_workflow.py::test_platform_harness_node_budget_blocks_run tests/test_workflow.py::test_builder_benchmark_suite_reports_aggregate_trends_and_harness_usage tests/test_workflow.py::test_platform_harness_tasks_persist_across_app_instances -q
```

Result:

- `4 passed, 1 warning`

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py
.venv/bin/python -m ruff check platform/backend/src/agent_platform/storage.py platform/backend/src/agent_platform/platform_harness.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `58 passed, 1 warning`

Paid/live model test:

- Not required for this stage. The change is deterministic storage and API retrieval behavior; no Builder/model generation quality or provider behavior changed.

## Current Decision

Proceed to archive `v0.2.10` and recycle `design_platform_harness_durable_storage_v1.md` into historical design records.
