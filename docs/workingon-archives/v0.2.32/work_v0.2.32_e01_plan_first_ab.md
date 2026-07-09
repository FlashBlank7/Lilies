# work_v0.2.32_e01_plan_first_ab

## 1. Goal

Run the original E01 plan-first vs node-by-node experiment with a controlled planning-mode switch, bounded paid/live calls, DOCX reporting, and experiment-ledger updates.

## 2. Scope

Included:

- Add a narrow Builder `planning_mode` switch for experiments: `auto`, `required`, `disabled`.
- Use `required` as plan-first and `disabled` as node-by-node baseline.
- Run the same requirement family through both modes with bounded paid/live calls.
- Produce an E01 A/B DOCX report and update the experiment ledger.

Excluded:

- Closing E02/E04/E05/E08.
- Turning planning mode into a frontend product control.
- Broad Builder prompt redesign beyond the experiment switch.

## 3. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.31_builder_repair_confirmation.md`

| Prior next-stage task | Disposition | Current design | Reason |
| --- | --- | --- | --- |
| Run E01 plan-first vs node-by-node A/B | Accepted | `docs/current-design/design_v0.2.32_planning_mode_switch_v1.md`; `docs/current-design/design_v0.2.32_e01_paid_ab_v1.md` | v0.2.31 unblocked this original experiment. |
| Run E02 human-review experiment | Deferred | none | Separate experiment stage. |
| Run E04 local repair vs full rebuild comparison | Deferred | none | Separate experiment stage. |
| Run E05 none/shallow/deep reuse-depth live generation comparison | Deferred | none | Separate experiment stage. |
| Run E08 workflow-internal gate vs sidecar monitor comparison | Deferred | none | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | Deferred | none | Separate Platform Harness stage. |

## 4. Execution Evidence

Implementation:

- `platform/backend/src/agent_platform/workflow_models.py`: added `planning_mode` to `BuildRequest` and `BuildTeamState`.
- `platform/backend/src/agent_platform/workflow_storage.py`: persists planning mode inside `BuildTeamState`.
- `platform/backend/src/agent_platform/api.py`: passes `planning_mode` from build request.
- `platform/backend/src/agent_platform/builder.py`: adds planning-mode prompt, disables `build_plan` in disabled mode, and enforces `build_plan` before mutation in required mode.
- `scripts/live_builder_benchmark_suite.py`: accepts `LIVE_BUILDER_BENCHMARK_PLANNING_MODE` and records it in result JSON.

Planning-mode focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_persists_plan_first_build_plan tests/test_workflow.py::test_builder_planning_mode_required_blocks_mutation_before_plan tests/test_workflow.py::test_builder_planning_mode_disabled_rejects_build_plan_tool -q
```

Result: `3 passed, 1 warning`.

Paid/live A/B:

```bash
LIVE_BUILDER_BENCHMARK_PLANNING_MODE=required \
LIVE_BUILDER_BENCHMARK_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.32_e01_plan_first_required_2026_07_09.json \
LIVE_BUILDER_BENCHMARK_MAX_TURNS=24 \
LIVE_BUILDER_BENCHMARK_TIMEOUT_SECONDS=900 \
.venv/bin/python scripts/live_builder_benchmark_suite.py
```

Result: `status=passed`, `build_status=ready`, benchmark score `0.85`, pass_rate `1.0`, model calls `21`, tool calls `30`, duration `183.7s`, build_plan calls `6`.

```bash
LIVE_BUILDER_BENCHMARK_PLANNING_MODE=disabled \
LIVE_BUILDER_BENCHMARK_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.32_e01_node_by_node_disabled_2026_07_09.json \
LIVE_BUILDER_BENCHMARK_MAX_TURNS=24 \
LIVE_BUILDER_BENCHMARK_TIMEOUT_SECONDS=900 \
.venv/bin/python scripts/live_builder_benchmark_suite.py
```

Result: `status=passed`, `build_status=ready`, benchmark score `0.85`, pass_rate `1.0`, model calls `18`, tool calls `25`, duration `87.4s`, build_plan calls `0`.

Generated evidence:

- `docs/experiment-status/evidence/experiment_v0.2.32_e01_plan_first_required_2026_07_09.json`
- `docs/experiment-status/evidence/experiment_v0.2.32_e01_node_by_node_disabled_2026_07_09.json`
- `docs/experiment-status/evidence/experiment_v0.2.32_e01_ab_summary_2026_07_09.json`
- `docs/experiment-status/reports/2026-07-09_1834_E01_plan_first_vs_node_by_node_ab.docx`

DOCX QA:

- Zip structure check: passed.
- Paragraph/readback check: required sections exist.
- Visual render QA: skipped because `soffice` / `libreoffice` is unavailable.

## 5. Design Execution Status

| Design | Status | Evidence | Proceed |
| --- | --- | --- | --- |
| `design_v0.2.32_planning_mode_switch_v1.md` | Completed | Planning mode implemented and focused tests passed | yes |
| `design_v0.2.32_e01_paid_ab_v1.md` | Completed | Paid/live A/B completed; DOCX report and ledger updated | yes |

## 6. Completion Gate

- All source tasks dispositioned: yes
- All accepted tasks expanded into design: yes
- All accepted designs completed or explicitly blocked/deferred: yes
- Planning mode deterministic tests passed: yes
- Paid/live A/B completed: yes
- E01 DOCX report created: yes
- Experiment ledger updated: yes
- This file records current stage evidence only and does not guide the next stage: yes
