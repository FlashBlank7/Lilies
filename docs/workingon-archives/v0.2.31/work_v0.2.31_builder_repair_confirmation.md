# work_v0.2.31_builder_repair_confirmation

## 1. Goal

Fix the Builder repair-cycle boundary exposed by the v0.2.29 paid/live run: after one failed `test_run`, Builder repaired the draft/test, but a confirming `test_run` was blocked by `maximum repair cycles reached (1)`.

## 2. Scope

Included:

- Add a state marker for the draft revision that last failed tests.
- Allow one confirmation test run after the draft revision changes, even when `repair_cycles == max_repair_cycles`.
- Add a deterministic Builder test that fails once, repairs the test, then confirms successfully with `max_repair_cycles=1`.
- Reuse the v0.2.29 paid result after the fix if possible.
- Update experiment status as an applied supplement to E01.

Excluded:

- Fresh paid Builder generation.
- E01 plan-first vs node-by-node A/B.
- Broad Builder prompt redesign.
- E02/E04/E05/E08 comparison experiments.

## 3. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.30_builder_terminal_node_repair.md`

| Prior next-stage task | Disposition | Current design | Reason |
| --- | --- | --- | --- |
| Fix or model Builder output-contract behavior for workflow-mode named outputs | Accepted as repair confirmation boundary fix | `docs/current-design/design_v0.2.31_repair_confirmation_boundary_v1.md`; `docs/current-design/design_v0.2.31_paid_result_recheck_v1.md` | Evidence shows the test was repaired, but confirmation was blocked by repair-cycle accounting. |
| Run E01 plan-first vs node-by-node A/B | Deferred | none | Must wait until repair confirmation boundary is fixed. |
| Run E02 human-review experiment | Deferred | none | Separate experiment stage. |
| Run E04 local repair vs full rebuild comparison | Deferred | none | Separate experiment stage. |
| Run E05 none/shallow/deep reuse-depth live generation comparison | Deferred | none | Separate experiment stage. |
| Run E08 workflow-internal gate vs sidecar monitor comparison | Deferred | none | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | Deferred | none | Separate Platform Harness stage. |

## 4. Execution Evidence

v0.2.29 event evidence:

- First `test_run` failed at revision `6` because assertions expected `summary`.
- Builder later changed the test to assert `answer` and updated the draft/test to revision `8`.
- Subsequent `test_run` calls failed with `RuntimeError: maximum repair cycles reached (1)`.

Implementation:

- `platform/backend/src/agent_platform/workflow_models.py`: added `BuildTeamState.last_failed_test_revision`.
- `platform/backend/src/agent_platform/builder.py`: changed `test_run` repair-cycle guard so a changed revision can be confirmed, while repeated tests at the same failed revision remain blocked.
- `tests/test_workflow.py`: added `RepairConfirmationBuilderProvider` and `test_builder_allows_confirmation_test_after_repair_revision`.

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_allows_confirmation_test_after_repair_revision tests/test_workflow.py::test_builder_uses_incremental_brick_operations_and_publishes tests/test_workflow.py::test_builder_rejects_tests_requiring_unavailable_node_types -q
```

Result: `3 passed, 1 warning`.

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result: `87 passed, 1 warning`.

Static compile:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
```

Result: passed.

Fresh paid/live rerun:

```bash
LIVE_BUILDER_BENCHMARK_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.31_paid_builder_repair_confirmation_2026_07_09.json \
LIVE_BUILDER_BENCHMARK_MAX_TURNS=24 \
LIVE_BUILDER_BENCHMARK_TIMEOUT_SECONDS=900 \
.venv/bin/python scripts/live_builder_benchmark_suite.py
```

Result:

- Evidence: `docs/experiment-status/evidence/experiment_v0.2.31_paid_builder_repair_confirmation_2026_07_09.json`
- Summary: `docs/experiment-status/evidence/experiment_v0.2.31_repair_confirmation_summary_2026_07_09.json`
- Build status: `ready`
- Script status: `passed`
- Benchmark passed: `true`
- Score: `0.85`
- Pass rate: `1.0`
- Model calls: `20`
- Tool calls: `44`
- Draft node types: `start`, `llm`, `end`

Experiment report supplement:

- `docs/experiment-status/reports/2026-07-09_1809_E01_plan_first_paid_builder_baseline.docx` now includes `v0.2.31 repair confirmation supplement`.
- `docs/experiment-status/v0.2_experiment_status.md` records the v0.2.31 applied marker and fresh paid/live evidence.

## 5. Design Execution Status

| Design | Status | Evidence | Proceed |
| --- | --- | --- | --- |
| `design_v0.2.31_repair_confirmation_boundary_v1.md` | Completed | Guard implemented; focused and full backend tests passed | yes |
| `design_v0.2.31_paid_result_recheck_v1.md` | Completed | Fresh paid/live rerun reached `ready`; E01 report and ledger supplemented | yes |

## 6. Completion Gate

- All source tasks dispositioned: yes
- All accepted tasks expanded into design: yes
- All accepted designs completed or explicitly blocked/deferred: yes
- Focused deterministic tests passed: yes
- Full backend regression passed: yes
- Paid result reused/rechecked without new paid call: superseded by fresh bounded paid/live rerun because old build was terminal
- Experiment ledger/report updated if applied: yes
- This file records current stage evidence only and does not guide the next stage: yes
