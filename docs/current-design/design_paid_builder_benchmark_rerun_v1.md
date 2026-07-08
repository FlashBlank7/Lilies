# design_paid_builder_benchmark_rerun_v1

## 1. Goal

Rerun the paid Builder benchmark after v0.2.7 so the project has real evidence about whether Builder test self-consistency improved live behavior.

## 2. Module Boundary

Code/script:

- `scripts/live_builder_benchmark_suite.py`

Docs/evidence:

- `docs/workingon/work_paid_builder_benchmark_rerun_2026_07_09.md`
- `docs/workingon/experiment_builder_test_self_consistency_rerun_2026_07_09.json`
- `docs/workingon/experiment-reports/2026-07-09_builder_test_self_consistency_rerun.docx`
- `docs/stage-reports/v0.2.8_paid_builder_benchmark_rerun.md`

## 3. Control Flow

```text
set LIVE_BUILDER_BENCHMARK_RESULT_PATH
  -> run scripts/live_builder_benchmark_suite.py
  -> one paid Builder build
  -> benchmark suite evaluation when a draft exists
  -> write rerun JSON
  -> generate rerun DOCX report
```

## 4. Implementation Plan

1. Make result path configurable by environment variable.
2. Run the same paid benchmark without `LIVE_BUILDER_BENCHMARK_REUSE_RESULT`.
3. Compare status, usage, build error, draft counts, and benchmark score with v0.2.6.
4. Generate DOCX report.
5. Archive and commit.

## 5. Acceptance Criteria

- New result file exists and has application/build IDs.
- The old v0.2.6 result file remains unchanged.
- The rerun status is recorded even if not successful.
- The report has background, experiment design, result, and conclusion.

## 6. Referenced Evidence

- `docs/stage-reports/v0.2.6_paid_builder_benchmark_experiment.md`
- `docs/stage-reports/v0.2.7_builder_test_self_consistency.md`

## 7. Implementation Result

Status: implemented as a completed paid rerun.

Implemented code:

- `scripts/live_builder_benchmark_suite.py`

Experiment outputs:

- `docs/workingon/experiment_builder_test_self_consistency_rerun_2026_07_09.json`
- `docs/workingon/experiment-reports/2026-07-09_builder_test_self_consistency_rerun.docx`

Result:

- Build reached `ready`.
- Builder usage improved from `model_call=36/tool_call=54` to `model_call=18/tool_call=35`.
- Benchmark failed with score `0.733` because the candidate used `llm` while the suite required `model_turn`.

Conclusion:

- v0.2.7 fixed the live Builder stopping failure.
- v0.2.8 exposed a benchmark design issue: node type equivalence should be supported for semantically similar blocks.

