# v0.2.33 E01 Complex BlockFlow A/B

## Goal

Complete the next E01 slice from `docs/stage-reports/v0.2.32_e01_plan_first_ab.md`: run a bounded paid/live A/B on a complex multi-module BlockFlow using the planning-mode switch introduced in v0.2.32.

This stage must not close original E01 unless the complex experiment has real evidence, a concise DOCX report, and the experiment ledger is updated honestly.

## Source Task Set Disposition

| Prior next-stage task | v0.2.33 disposition | Reason |
| --- | --- | --- |
| Run E01 complex multi-module A/B using planning-mode switch | accepted | Direct continuation of v0.2.32; required before closing plan-first question. |
| Run E02 human-review experiment | deferred | Separate experiment stage; not mixed with E01. |
| Run E04 local repair vs full rebuild comparison | deferred | Separate experiment stage. |
| Run E05 none/shallow/deep reuse-depth live generation comparison | deferred | Separate experiment stage. |
| Run E08 workflow-internal gate vs sidecar monitor comparison | deferred | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | deferred | Separate engineering/product stage. |

## Linked Designs

- `docs/current-design/design_v0.2.33_complex_benchmark_case_v1.md`
- `docs/current-design/design_v0.2.33_e01_complex_paid_ab_v1.md`

## Execution Plan

| Step | Status | Evidence |
| --- | --- | --- |
| Extend live benchmark script with selectable benchmark cases | completed | `scripts/live_builder_benchmark_suite.py` case registry |
| Define complex multi-module reference BlockFlow | completed | `complex_research_brief_reference_workflow()` |
| Add deterministic tests for case selection/reference semantics | completed | `3 passed, 1 warning` focused pytest |
| Run paid/live `planning_mode=required` complex case | completed | `docs/experiment-status/evidence/experiment_v0.2.33_e01_complex_required_2026_07_09.json` |
| Run paid/live `planning_mode=disabled` complex case | completed | `docs/experiment-status/evidence/experiment_v0.2.33_e01_complex_disabled_2026_07_09.json` |
| Generate concise DOCX experiment report | completed | `docs/experiment-status/reports/2026-07-09_1956_E01_complex_plan_first_vs_node_by_node_ab.docx` |
| Update experiment ledger without over-closing E01 | completed | `docs/experiment-status/v0.2_experiment_status.md` |
| Archive stage designs and workingon by version | pending | historical designs and stage report |

## Acceptance Criteria

- The benchmark script can run at least `summary_smoke` and `complex_research_brief` without editing source code.
- Complex reference workflow requires multiple visible node types beyond `start/model/end`, including at least extraction, classification, transformation, aggregation, and a soft harness observation/guard node where appropriate.
- Both A/B runs use configured paid model keys and bounded budgets.
- The result report states whether plan-first improved quality, cost, or readiness for the complex case, and states limitations.
- Active `docs/current-design/` and `docs/workingon/` are empty except README after archive.

## Current Status

Design 1 completed. Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_live_builder_benchmark_case_registry_supports_complex_case tests/test_workflow.py::test_builder_benchmark_treats_llm_as_model_turn_equivalent tests/test_workflow.py::test_builder_benchmark_treats_answer_as_terminal_end_equivalent -q
```

Result: `3 passed, 1 warning`.

Compile:

```bash
.venv/bin/python -m compileall -q scripts/live_builder_benchmark_suite.py tests/test_workflow.py
```

Result: passed.

Paid/live complex A/B evidence:

| Metric | planning_mode=required | planning_mode=disabled |
| --- | --- | --- |
| Build status | `needs_attention` | `needs_attention` |
| Build error | `builder stopped before mandatory tests passed` | `invalid tool input JSON for draft_add_node` |
| Benchmark score | `0.85` | `0.475` |
| Pass rate | `1.0` | `0.0` |
| Draft nodes / edges / tests | `9 / 8 / 3` | `5 / 0 / 0` |
| Model calls | `41` | `15` |
| Tool calls | `98` | `52` |

Summary:

- `planning_mode=required` produced the complete expected visible architecture and passed benchmark coverage, but failed Builder readiness because mandatory tests did not pass.
- `planning_mode=disabled` was faster and cheaper, but did not complete the graph and failed benchmark coverage.
- E01 is not closed. The next concrete question is why the structurally complete required-mode BlockFlow fails mandatory tests.
