# work_builder_benchmark_history_2026_07_09

## Goal

推进 `v0.2.13_builder_benchmark_history`：在 v0.2.10 durable Platform Harness tasks 之上，为 Builder benchmark 增加可查询历史记录接口。

## Scope

包含：

- 新增只读 API：`GET /api/v1/builder-benchmark/history`。
- 从 Platform Harness `kind=benchmark` task records 生成轻量 history item。
- 支持 `owner_id`、`status`、`limit` 过滤。
- 增加跨 app instance 的 benchmark history 回归测试。

不包含：

- 新的 benchmark 结果表。
- 前端 UI。
- benchmark report 详情重放。
- paid model benchmark rerun。

## Linked Current Design

- `docs/current-design/design_builder_benchmark_history_v1.md`

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Audit v0.2.12 design archive gate | completed |
| 2 | Add benchmark history API | completed |
| 3 | Add cross-instance regression test | completed |
| 4 | Run focused and full verification | completed |
| 5 | Archive v0.2.13 with design recycling | completed |

## Acceptance Criteria

- History endpoint returns durable benchmark tasks after app recreation.
- History items include task id, owner, resource/name, status, timestamps, metadata, and usage counts.
- Existing benchmark evaluate endpoints remain unchanged.
- Focused and full backend tests pass.

## Current Decision

Use Platform Harness task records as the history source. A dedicated benchmark result store can be added later only if report detail replay becomes necessary.

## Implementation Evidence

- API added:
  - `GET /api/v1/builder-benchmark/history`
- Filters:
  - `owner_id`
  - `status`
  - `limit`
- Regression test added:
  - `test_builder_benchmark_history_survives_app_recreation`

Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_benchmark_history_survives_app_recreation tests/test_workflow.py::test_platform_harness_tasks_persist_across_app_instances tests/test_workflow.py::test_builder_benchmark_suite_reports_aggregate_trends_and_harness_usage -q
```

Result:

- `3 passed, 1 warning`

Full verification:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py
.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `61 passed, 1 warning`

Paid/live model test:

- Not required. This stage adds deterministic read-only history retrieval over persisted benchmark task records.
