# work_paid_builder_benchmark_rerun_2026_07_09

## Goal

在 Automatic Evolution Mode 下推进 `v0.2.8_paid_builder_benchmark_rerun`。

目标是在 v0.2.7 Builder 测试自洽性修复之后，重新运行同一个有界付费 Builder benchmark，验证真实 Builder 行为是否从“最终 validate 才失败”变成“更早收到工具反馈并尝试修复”。

## Scope

Included:

- Update live benchmark runner to support custom result paths.
- Run one paid Builder benchmark rerun after v0.2.7 fix.
- Preserve v0.2.6 original experiment evidence.
- Produce a new `.docx` report for the rerun.

Excluded:

- Multiple paid trials.
- Durable benchmark history.
- Frontend benchmark dashboard.

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Create current design | completed |
| 2 | Add custom result path support to runner | completed |
| 3 | Run one paid rerun | completed |
| 4 | Generate rerun DOCX report | completed |
| 5 | Verify and archive stage | in progress |

## Linked Current Design

- `docs/historical-designs/v0.2.8_design_paid_builder_benchmark_rerun_v1.md`

## Acceptance Criteria

- The rerun writes to `docs/experiment-status/evidence/experiment_builder_test_self_consistency_rerun_2026_07_09.json`.
- The v0.2.6 result file is not overwritten.
- The report explicitly compares the rerun with v0.2.6.
- If Builder still fails, the failure mode is recorded as the next engineering input.

## Current Decision

Proceed to next design: yes. The rerun completed and exposed the next boundary: benchmark node type equivalence.

## Implementation Evidence

- Updated `scripts/live_builder_benchmark_suite.py` to support `LIVE_BUILDER_BENCHMARK_RESULT_PATH`.
- Ran one paid rerun after v0.2.7 self-consistency fix.
- Rerun result: `build_status=ready`, `status=benchmark_failed`.
- Builder usage improved from `model_call=36/tool_call=54` to `model_call=18/tool_call=35`.
- Benchmark failed because candidate used `llm` while the suite required `model_turn`.
- Generated DOCX report at `docs/experiment-status/reports/2026-07-09_builder_test_self_consistency_rerun.docx`.

Key evidence:

```text
build_status: ready
benchmark_score: 0.733
missing_node_types: model_turn
candidate_node_types: end, llm, start
```

Verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_rejects_tests_requiring_unavailable_node_types tests/test_workflow.py::test_builder_benchmark_suite_reports_aggregate_trends_and_harness_usage -q
.venv/bin/python -m ruff check scripts/live_builder_benchmark_suite.py platform/backend/src/agent_platform/builder.py tests/test_workflow.py
unzip -t docs/experiment-status/reports/2026-07-09_builder_test_self_consistency_rerun.docx
```

Result:

- Focused tests: `2 passed`.
- Focused ruff: passed.
- DOCX package structure valid.

DOCX QA boundary:

- Full PNG render QA remains blocked because local `soffice` is unavailable.
