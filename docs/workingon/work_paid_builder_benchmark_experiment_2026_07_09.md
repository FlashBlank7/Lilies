# work_paid_builder_benchmark_experiment_2026_07_09

## Goal

在 Automatic Evolution Mode 下推进 `v0.2.6_paid_builder_benchmark_experiment`。

目标是用真实配置的付费 Builder Team 生成一个小型 `BlockFlow`，把生成出的 `WorkflowSpec` 作为 candidate 喂入 v0.2.5 的 benchmark suite endpoint，并产出简明 `.docx` 实验报告。

## Scope

Included:

- Create a bounded live experiment runner.
- Use one paid Builder build with conservative `max_turns`.
- Evaluate the generated draft through `POST /api/v1/builder-benchmark/suites/evaluate`.
- Store structured experiment output in `docs/workingon/`.
- Produce a concise `.docx` experiment report with background, design, result, and conclusion.

Excluded:

- Large benchmark set.
- Repeated paid trials.
- Durable benchmark history.
- Frontend benchmark dashboard.

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Confirm local backend and credentials | completed |
| 2 | Create current design for paid benchmark experiment | completed |
| 3 | Implement bounded live runner script | completed |
| 4 | Run one paid Builder benchmark experiment | completed |
| 5 | Generate docx experiment report | completed |
| 6 | Verify and archive stage | in progress |

## Linked Current Design

- `docs/current-design/design_paid_builder_benchmark_experiment_v1.md`

## Acceptance Criteria

- Experiment runner uses local API token without printing secrets.
- Experiment creates one application and one bounded Builder build.
- Experiment records build status, model/provider metadata, draft node/edge/test counts, suite score, pass rate, failed cases, task IDs, and skip/failure reason if any.
- Experiment produces `docs/workingon/experiment-reports/2026-07-09_paid_builder_benchmark_experiment.docx`.
- If the live build cannot complete due to provider/service failure, the failure is still captured as an experiment result, not silently ignored.

## Current Decision

Proceed to next design: yes. The paid experiment completed and produced a concrete Builder failure mode for the next stage.

## Implementation Evidence

- Implemented `scripts/live_builder_benchmark_suite.py`.
- Ran one paid Builder build through real configured provider in in-process TestClient mode.
- First live run used `max_turns=36`, `max_repair_cycles=1`, `auto_publish=false`.
- Build reached `needs_attention`.
- Failure reason: `builder stopped with invalid draft: test test_summary_exists missing required node types: ['extract_text']`.
- Reused the same generated draft for deterministic suite evaluation without a second paid build.
- Benchmark suite evaluation succeeded and scored the candidate `0.85`.
- Experiment report generated at `docs/workingon/experiment-reports/2026-07-09_paid_builder_benchmark_experiment.docx`.

Key evidence:

```text
status: build_failed_benchmark_evaluated
builder_usage: model_call=36, tool_call=54
draft_counts: 5 nodes, 4 edges, 1 test
draft_node_types: end, model_turn, start, template_transform
benchmark_score: 0.85
benchmark_passed: true
```

Verification:

```bash
.venv/bin/python -m ruff check scripts/live_builder_benchmark_suite.py
LIVE_BUILDER_BENCHMARK_REUSE_RESULT=1 .venv/bin/python scripts/live_builder_benchmark_suite.py
unzip -t docs/workingon/experiment-reports/2026-07-09_paid_builder_benchmark_experiment.docx
```

DOCX QA boundary:

- DOCX structural check passed.
- Full render-to-PNG QA could not be completed because local `soffice` is not installed.
