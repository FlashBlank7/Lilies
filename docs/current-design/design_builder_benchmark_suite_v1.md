# design_builder_benchmark_suite_v1

## 1. Goal

Upgrade Builder benchmark v1 from a single-case structural scorer into a suite-level evaluator that can become the stable entrypoint for future paid Builder Team regression experiments.

In Lilies language, this design evaluates whether candidate `BlockFlow` outputs, represented as `WorkflowSpec`, preserve required structure, tools, Harness nodes and readable test frames across multiple requirements.

## 2. Module Boundary

Code modules:

- `platform/backend/src/agent_platform/builder_benchmark.py`
- `platform/backend/src/agent_platform/api.py`
- `tests/test_workflow.py`

Documentation modules:

- `docs/workingon/work_builder_benchmark_suite_2026_07_09.md`
- `docs/stage-reports/v0.2.5_builder_benchmark_suite.md`
- `docs/historical-designs/v0.2.5_design_builder_benchmark_suite_v1.md`

## 3. Data Flow

```text
BuilderBenchmarkSuiteCase
  -> BuilderBenchmark.evaluate_suite()
  -> per-case BuilderBenchmarkReport[]
  -> aggregate score / pass rate / failed cases / trend / cost metadata
  -> BuilderBenchmarkSuiteReport
```

API flow:

```text
POST /api/v1/builder-benchmark/suites/evaluate
  -> PlatformHarness.start_task(kind=benchmark)
  -> PlatformHarness.record_usage(node_execution, case_count)
  -> BuilderBenchmark.evaluate_suite()
  -> PlatformHarness.finish_task(succeeded|failed)
  -> response { task_id, report }
```

## 4. Implementation Plan

1. Add `BuilderBenchmarkCostRecord`, `BuilderBenchmarkTrend`, `BuilderBenchmarkSuiteCase`, and `BuilderBenchmarkSuiteReport`.
2. Implement `BuilderBenchmark.evaluate_suite()`.
3. Add `POST /api/v1/builder-benchmark/suites/evaluate`.
4. Record deterministic evaluator work as Platform Harness usage.
5. Add tests for mixed pass/fail suite, aggregate values, trends, and task usage.
6. Update docs indexes and archive the design after stage report exists.

## 5. Acceptance Criteria

- Suite evaluation accepts at least two cases and returns per-case reports.
- Suite report has `score`, `pass_rate`, `failed_cases`, `metrics`, `trends`, and `cost`.
- Suite passes only when average score and pass rate meet thresholds.
- Harness task metadata contains suite name and case count.
- Harness usage contains benchmark suite work evidence.

## 6. Referenced Intellectual Assets

- `docs/intellectual-assets/asset_blockflow_language_system.md`
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`

## 7. Paid Model Boundary

This design creates the deterministic suite container. It does not invoke Builder Team generation by itself.

Bounded paid/live model acceptance should be run in the next experiment that feeds real Builder-generated `WorkflowSpec` outputs into this suite. If credentials and service state permit a small live smoke during this stage, record the command and evidence in the workingon implementation section.

## 8. Implementation Result

Status: implemented.

Implemented code:

- `platform/backend/src/agent_platform/builder_benchmark.py`
- `platform/backend/src/agent_platform/api.py`
- `tests/test_workflow.py`

Public endpoint:

- `POST /api/v1/builder-benchmark/suites/evaluate`

Report shape:

- `score`
- `pass_rate`
- `failed_cases`
- `reports`
- `metrics.average`
- `trends`
- `cost`

Verification:

- Focused benchmark tests passed: `2 passed`.
- Full backend regression passed: `55 passed, 1 warning`.
- Focused ruff passed.

Boundary:

- Deterministic suite evaluation is implemented.
- Durable benchmark history and paid live Builder generation experiment are not implemented in this stage.

