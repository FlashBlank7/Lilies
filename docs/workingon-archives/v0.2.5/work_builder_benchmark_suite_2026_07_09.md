# work_builder_benchmark_suite_2026_07_09

## Goal

在 Automatic Evolution Mode 下推进 `v0.2.5_builder_benchmark_suite`，把现有单 case Builder benchmark v1 升级为可配置 suite。

目标不是立刻证明 Builder Team 质量已经工业可用，而是先建立稳定的评估容器：多个需求 case、聚合分数、回归趋势、成本字段和 Platform Harness task 记录。

## Scope

Included:

- Extend `BuilderBenchmark` from single-case evaluation to suite evaluation.
- Add API endpoint for suite evaluation.
- Record suite evaluation through Platform Harness `benchmark` task.
- Add deterministic tests for multi-case aggregate, trend, and Harness usage evidence.
- Archive the design into `historical-designs` after stage state exists.

Excluded:

- Running a large paid Builder benchmark set.
- Durable benchmark history storage.
- Frontend benchmark dashboard.
- Automatic Builder Team generation for every suite case.

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Read benchmark v1, Platform Harness, API and tests | completed |
| 2 | Create current design for suite API and scoring | completed |
| 3 | Implement suite request/report models and evaluator | completed |
| 4 | Add API endpoint under Platform Harness task boundary | completed |
| 5 | Add deterministic tests | completed |
| 6 | Run verification and record evidence | completed |
| 7 | Archive stage and commit automatically | in progress |

## Linked Current Design

- `docs/historical-designs/v0.2.5_design_builder_benchmark_suite_v1.md`

## Acceptance Criteria

- A caller can evaluate multiple `BuilderBenchmarkCase` objects in one request.
- Suite report includes pass rate, average score, failed case names, per-case reports, regression trend against optional baseline scores, and cost metadata.
- API endpoint starts and finishes a Platform Harness `benchmark` task.
- Tests cover mixed pass/fail suite behavior and Harness task usage.
- Focused backend verification passes.

## Current Decision

Proceed to next design: yes. The current design has code changes, verification evidence, and remaining risk recorded below.

## Implementation Evidence

- Implemented `BuilderBenchmarkSuiteCase`, `BuilderBenchmarkSuiteReport`, `BuilderBenchmarkCostRecord`, and `BuilderBenchmarkTrend`.
- Implemented `BuilderBenchmark.evaluate_suite()`.
- Added `POST /api/v1/builder-benchmark/suites/evaluate`.
- Suite evaluation is recorded as a Platform Harness `benchmark` task owned by `builder-benchmark-suite`.
- Added deterministic mixed pass/fail suite test with aggregate, trend, cost, and Harness usage assertions.

Verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_benchmark_reports_missing_harness_nodes tests/test_workflow.py::test_builder_benchmark_suite_reports_aggregate_trends_and_harness_usage -q
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/builder_benchmark.py tests/test_workflow.py
```

Result:

- Focused benchmark tests passed: `2 passed`.
- Full backend tests passed: `55 passed, 1 warning`.
- Focused ruff passed.

Paid/live model boundary:

- This stage changed the deterministic suite container and API, not the Builder Team generation loop.
- No paid model call was made in this stage because the new endpoint does not invoke a provider.
- The next stage should run a bounded paid Builder benchmark experiment by feeding real Builder-generated `WorkflowSpec` candidates into this suite and writing a concise `.docx` experiment report.
