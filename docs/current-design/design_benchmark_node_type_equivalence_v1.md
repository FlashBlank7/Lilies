# design_benchmark_node_type_equivalence_v1

## 1. Goal

Make Builder benchmark scoring evaluate semantic roles rather than only literal node type strings when appropriate.

The concrete v0.2.8 case: a live Builder candidate used `llm`, while the benchmark required `model_turn`. For the summary task, both represent a model-step role, so this should not be a missing required node type.

## 2. Module Boundary

Code:

- `platform/backend/src/agent_platform/builder_benchmark.py`
- `tests/test_workflow.py`

No API route change is required because the evaluator can apply default equivalence internally.

## 3. Data Flow

```text
required_node_types
  -> candidate node types
  -> equivalent node type map
  -> satisfied/missing required roles
  -> node_type_coverage and missing.node_types
```

## 4. Implementation Plan

1. Add default equivalence map in `BuilderBenchmark`, starting with `model_turn -> llm`.
2. Add optional case-level `equivalent_node_types` override for future benchmark cases.
3. Use equivalence when computing missing node types and coverage.
4. Add a regression test where candidate `llm` satisfies required `model_turn`.

## 5. Acceptance Criteria

- Benchmark report passes for `required_node_types=["start", "model_turn", "end"]` when candidate has `start`, `llm`, `end`.
- Missing values remain visible when no equivalent node exists.
- Full backend regression passes.

## 6. Referenced Evidence

- `docs/stage-reports/v0.2.8_paid_builder_benchmark_rerun.md`
- `docs/workingon/experiment_builder_test_self_consistency_rerun_2026_07_09.json`

## 7. Implementation Result

Status: implemented.

Implemented code:

- `platform/backend/src/agent_platform/builder_benchmark.py`
- `tests/test_workflow.py`
- `scripts/live_builder_benchmark_suite.py`

Behavior:

- Default equivalence map includes `model_turn -> llm`.
- Case-level `equivalent_node_types` can extend defaults.
- Missing node type reporting now treats equivalents as satisfied.

Verification:

- Focused benchmark tests passed: `3 passed`.
- Full backend tests passed: `57 passed, 1 warning`.
- Focused ruff passed.
- v0.2.8 candidate re-evaluation passed with score `0.85`.

Experiment report:

- `docs/workingon/experiment-reports/2026-07-09_benchmark_node_type_equivalence_recheck.docx`

