# design_paid_builder_benchmark_experiment_v1

## 1. Goal

Run a bounded paid/live Builder benchmark experiment that connects three project mechanisms:

1. Builder Team creates a `BlockFlow`.
2. v0.2.5 benchmark suite evaluates the resulting `WorkflowSpec`.
3. The experiment is recorded as working evidence and summarized in a `.docx` report.

## 2. Module Boundary

Code / script:

- `scripts/live_builder_benchmark_suite.py`

Docs:

- `docs/workingon/work_paid_builder_benchmark_experiment_2026_07_09.md`
- `docs/workingon/experiment_paid_builder_benchmark_result_2026_07_09.json`
- `docs/workingon/experiment-reports/2026-07-09_paid_builder_benchmark_experiment.docx`
- `docs/stage-reports/v0.2.6_paid_builder_benchmark_experiment.md`

No backend API change is planned for this stage.

## 3. Experiment Flow

```text
load .env API_TOKEN and provider config
  -> GET /health
  -> POST /api/v1/applications
  -> POST /api/v1/applications/{id}/builds
  -> poll /api/v1/builds/{build_id}
  -> GET /api/v1/applications/{id}/draft
  -> POST /api/v1/builder-benchmark/suites/evaluate
  -> write structured JSON result
  -> generate DOCX report
```

## 4. Benchmark Case Design

The live task should be small enough to finish in one bounded run but structured enough to evaluate:

- required node types: `start`, `model_turn`, `end`
- required test frames: at least one readable frame
- required harness nodes: none for this smoke, because the task is not permission-sensitive
- suite baseline: omitted for the first live run

The experiment result must separate:

- Builder build result
- benchmark suite result
- Platform Harness task evidence
- cost metadata when visible

## 5. Acceptance Criteria

- The runner fails closed when health says provider is not configured.
- The runner writes a JSON result even on live build failure.
- The runner evaluates the draft only when a `ready` or `published` build has produced a valid workflow.
- The DOCX report uses the required sections: background, experiment design, result, conclusion.
- The stage report records whether the paid experiment passed, failed, or was blocked.

## 6. Referenced Intellectual Assets

- `docs/intellectual-assets/asset_blockflow_language_system.md`
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`

## 7. Risk Boundary

The experiment is intentionally bounded to one Builder build. It should not run a loop of paid generations without an explicit later benchmark budget design.

## 8. Implementation Result

Status: implemented as a completed experiment.

Implemented script:

- `scripts/live_builder_benchmark_suite.py`

Experiment outputs:

- `docs/workingon/experiment_paid_builder_benchmark_result_2026_07_09.json`
- `docs/workingon/experiment-reports/2026-07-09_paid_builder_benchmark_experiment.docx`

Result:

- Paid Builder run reached `needs_attention`, not `ready`.
- Platform Harness evidence recovered from event logs: `model_call=36`, `tool_call=54`.
- Builder failure reason: mandatory test `test_summary_exists` required nonexistent node type `extract_text`.
- Generated draft still contained a structurally plausible candidate with node types `start`, `model_turn`, `template_transform`, and `end`.
- Benchmark suite evaluation succeeded: score `0.85`, passed `true`.

Conclusion:

- The benchmark suite can evaluate real Builder-generated candidates.
- Builder test generation / repair needs a self-consistency guard for `required_node_types` and `required_tool_nodes`.

DOCX QA:

- `.docx` was generated and passed archive/structure checks.
- Render QA was attempted with the Documents renderer, but `soffice` is unavailable on this machine.

