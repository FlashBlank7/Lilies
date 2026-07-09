# v0.2.36 E02 Readable TestFrame Human Review

## Goal

Run the next formal E02 experiment slice: compare raw JSON-style test output with readable TestFrame/report output for reviewer comprehension and repair usefulness.

This stage should be honest about evidence level. Without an actual human panel, the stage may complete a deterministic + paid model reviewer proxy experiment, but it must not claim to have completed a real human-subject study.

## Source Task Set Disposition

Source: `docs/stage-reports/v0.2.35_e01_required_architecture_coverage.md`

| Prior next-stage task | v0.2.36 disposition | Reason |
| --- | --- | --- |
| Run E02 human-review experiment | accepted | Recommended v0.2.36 handoff. |
| Run E04 local repair vs full rebuild comparison | deferred | Separate experiment stage. |
| Run E05 reuse-depth live generation comparison | deferred | Separate experiment stage. |
| Run E08 workflow-internal gate vs sidecar/passmode comparison | deferred | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | deferred | Separate Platform Harness stage. |
| Add more complex E01 plan-first cases | deferred | E01 current blocker closed; not required before E02. |

## Linked Designs

- `docs/current-design/design_v0.2.36_e02_review_packet_and_metrics_v1.md`
- `docs/current-design/design_v0.2.36_e02_paid_reviewer_proxy_and_report_v1.md`

## Execution Plan

| Step | Status | Evidence |
| --- | --- | --- |
| Build paired raw/readable review packets from actual `run_test_suite()` output | completed | `scripts/e02_readable_testframe_review_experiment.py`; JSON evidence |
| Compute deterministic readability/search-cost metrics | completed | `experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json` |
| Run bounded paid model reviewer proxy | completed | DeepSeek `deepseek-v4-pro`; JSON evidence |
| Generate concise DOCX experiment report | completed | `docs/experiment-status/reports/2026-07-09_2013_E02_readable_testframe_reviewer_proxy.docx` |
| Update experiment ledger | completed | `docs/experiment-status/v0.2_experiment_status.md` |
| Archive stage designs and workingon by version | pending | historical designs and stage report |

## Acceptance Criteria

- The experiment uses real `WorkflowRuntime.run_test_suite()` output, not hand-written report fragments.
- Raw and readable conditions are derived from the same failing tests.
- Paid model calls are bounded and recorded when credentials are available.
- The report distinguishes deterministic/proxy evidence from actual human review evidence.
- E02 is not fully closed as a human-review experiment unless the evidence actually supports that claim.
- Active `docs/current-design/` and `docs/workingon/` are empty except README after archive.

## Current Status

Experiment execution completed.

Evidence summary:

- actual `run_test_suite()` report: 2 mandatory tests, both failed, both with readable frames.
- deterministic metrics:
  - raw legacy JSON estimated paths: `18`
  - readable TestFrame estimated paths: `10`
  - raw serialized chars: `2433`
  - readable serialized chars: `5524`
- paid reviewer proxy:
  - provider/model: DeepSeek `deepseek-v4-pro`
  - raw proxy score: `0.375`
  - readable proxy score: `1.0`

Boundary:

- This is deterministic metrics plus paid model reviewer proxy evidence.
- It is not a real human-panel timing experiment, so E02 should be marked proxy-evidence complete, not fully human-review closed.
