# work_benchmark_node_type_equivalence_2026_07_09

## Goal

在 Automatic Evolution Mode 下推进 `v0.2.9_benchmark_node_type_equivalence`。

目标是修复 v0.2.8 付费重跑暴露的 benchmark 误判：真实 Builder 生成了 `llm` 节点，但 suite 要求 `model_turn`，导致候选已 ready 但 benchmark failed。

## Scope

Included:

- Add benchmark node type equivalence support.
- Make `llm` satisfy `model_turn` by default for model-step requirements.
- Add deterministic tests for equivalent node type coverage.
- Keep report missing fields meaningful.

Excluded:

- Another paid Builder run.
- Durable benchmark history.
- Frontend benchmark UI.

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Create current design | completed |
| 2 | Implement node type equivalence in benchmark evaluator | completed |
| 3 | Add deterministic regression test | completed |
| 4 | Run verification | completed |
| 5 | Archive and commit stage | in progress |

## Linked Current Design

- `docs/historical-designs/v0.2.9_design_benchmark_node_type_equivalence_v1.md`

## Acceptance Criteria

- A candidate with `llm` satisfies required node type `model_turn`.
- Existing missing-node behavior still fails for truly absent roles.
- Full backend tests pass.

## Current Decision

Proceed to next design: yes. Benchmark equivalence is implemented and v0.2.8 candidate re-evaluation now passes.

## Implementation Evidence

- Added default benchmark node type equivalence: `model_turn -> llm`.
- Added optional case-level `equivalent_node_types`.
- Added regression test `test_builder_benchmark_treats_llm_as_model_turn_equivalent`.
- Re-evaluated the v0.2.8 candidate without another paid Builder call.

Evidence:

```text
recheck_status: passed
build_status: ready
benchmark_score: 0.85
benchmark_passed: true
missing_node_types: []
```

Verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_benchmark_treats_llm_as_model_turn_equivalent tests/test_workflow.py::test_builder_benchmark_reports_missing_harness_nodes tests/test_workflow.py::test_builder_benchmark_suite_reports_aggregate_trends_and_harness_usage -q
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py scripts/live_builder_benchmark_suite.py
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check platform/backend/src/agent_platform/builder_benchmark.py scripts/live_builder_benchmark_suite.py tests/test_workflow.py
```

Result:

- Focused benchmark tests: `3 passed`.
- Full backend tests: `57 passed, 1 warning`.
- Focused ruff: passed.

Experiment report:

- `docs/experiment-status/reports/2026-07-09_benchmark_node_type_equivalence_recheck.docx`

DOCX QA boundary:

- DOCX package structure valid.
- Full PNG render QA remains blocked because local `soffice` is unavailable.
