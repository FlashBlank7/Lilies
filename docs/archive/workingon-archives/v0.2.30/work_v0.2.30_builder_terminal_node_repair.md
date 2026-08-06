# work_v0.2.30_builder_terminal_node_repair

## 1. Goal

Repair or explicitly model the terminal-node semantics exposed by v0.2.29 paid/live evidence: the candidate BlockFlow used `answer`, while the benchmark required `end`.

This stage is an engineering bugfix slice derived from an experiment. It must update the experiment ledger with an applied marker only if the code change directly addresses the observed failure mode.

## 2. Scope

Included:

- Decide and implement the correct benchmark treatment for terminal node equivalence.
- Add deterministic tests for `end` / `answer` benchmark semantics.
- Re-evaluate the v0.2.29 paid result without creating a new paid Builder run.
- Update experiment status to mark the narrow v0.2.29 finding as applied if the fix is verified.

Excluded:

- Full Builder prompt redesign.
- New paid A/B runs.
- E01 original plan-first vs node-by-node closure.
- E02/E04/E05/E08 original comparison experiments.

## 3. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.29_formal_experiment_tranche.md`

| Prior next-stage task | Disposition | Current design | Reason |
| --- | --- | --- | --- |
| Analyze and fix paid/live Builder terminal-node failure | Accepted | `docs/current-design/design_v0.2.30_terminal_node_semantics_v1.md`; `docs/current-design/design_v0.2.30_paid_result_recheck_v1.md` | This is the recommended v0.2.30 handoff. |
| Run real E01 A/B after terminal issue is fixed or modeled | Deferred | none | Must wait until terminal semantics are deterministic. |
| Run E02 human-review experiment | Deferred | none | Separate experiment stage. |
| Run E04 local repair vs full rebuild comparison | Deferred | none | Separate experiment stage. |
| Run E05 none/shallow/deep reuse-depth live comparison | Deferred | none | Separate experiment stage. |
| Run E08 workflow-internal gate vs sidecar monitor comparison | Deferred | none | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | Deferred | none | Separate Platform Harness stage. |

## 4. Execution Evidence

Initial code observation:

- `BlockRegistry.validate_workflow()` accepts terminal nodes of type `end` or `answer`.
- `WorkflowRuntime._terminal_outputs()` treats `end` and `answer` as terminal nodes.
- `BuilderBenchmark.DEFAULT_NODE_TYPE_EQUIVALENCE` only contains `model_turn -> llm`.
- v0.2.29 candidate node types were `start`, `model_turn`, and `answer`; benchmark missed required `end`.

Implementation:

- `platform/backend/src/agent_platform/builder_benchmark.py`: added default node type equivalence `end -> answer`.
- `tests/test_workflow.py`: added `test_builder_benchmark_treats_answer_as_terminal_end_equivalent`.

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_benchmark_treats_llm_as_model_turn_equivalent tests/test_workflow.py::test_builder_benchmark_treats_answer_as_terminal_end_equivalent -q
```

Result: `2 passed, 1 warning`.

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result: `86 passed, 1 warning`.

Paid result reuse / benchmark recheck:

```bash
LIVE_BUILDER_BENCHMARK_REUSE_RESULT=1 \
LIVE_BUILDER_BENCHMARK_REUSE_SOURCE_PATH=docs/experiment-status/evidence/experiment_v0.2.29_paid_builder_tranche_2026_07_09.json \
LIVE_BUILDER_BENCHMARK_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.30_terminal_node_recheck_2026_07_09.json \
.venv/bin/python scripts/live_builder_benchmark_suite.py
```

Result:

- Recheck evidence: `docs/experiment-status/evidence/experiment_v0.2.30_terminal_node_recheck_2026_07_09.json`
- Build status remains `needs_attention`.
- Benchmark passed: `true`.
- Score: `0.85`.
- Pass rate: `1.0`.
- Missing node types: `[]`.
- Equivalences: `end -> answer`, `model_turn -> llm`.

Experiment report supplement:

- `docs/experiment-status/reports/2026-07-09_1809_E01_plan_first_paid_builder_baseline.docx` now includes `工程应用补充`.
- `docs/experiment-status/v0.2_experiment_status.md` marks E01 as `已应用（窄修复）/ 原始 A/B 未关闭`.

## 5. Design Execution Status

| Design | Status | Evidence | Proceed |
| --- | --- | --- | --- |
| `design_v0.2.30_terminal_node_semantics_v1.md` | Completed | Equivalence implemented; focused tests and full backend regression passed | yes |
| `design_v0.2.30_paid_result_recheck_v1.md` | Completed | v0.2.29 paid result reused; benchmark recheck passed; ledger/report supplemented | yes |

## 6. Completion Gate

- All source tasks dispositioned: yes
- All accepted tasks expanded into design: yes
- All accepted designs completed or explicitly blocked/deferred: yes
- Focused deterministic tests passed: yes
- Paid result reused/rechecked without new paid call: yes
- Experiment ledger updated with applied marker if justified: yes
- This file records current stage evidence only and does not guide the next stage: yes
