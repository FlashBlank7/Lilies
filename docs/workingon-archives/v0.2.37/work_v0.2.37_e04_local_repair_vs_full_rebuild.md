# v0.2.37 E04 Local Repair vs Full Rebuild

## Goal

Run the E04 experiment slice: compare local repair against full Builder rebuild on a fixed failing draft.

The stage should answer a narrow question: when a BlockFlow is already mostly correct and the failure is localized, is a local patch cheaper/faster/more reliable than regenerating the whole workflow from the same requirement?

## Source Task Set Disposition

Source: `docs/stage-report-archives/v0.2.x/v0.2.36_e02_readable_testframe_human_review.md`

| Prior next-stage task | v0.2.37 disposition | Reason |
| --- | --- | --- |
| Run E04 local repair vs full rebuild comparison | accepted | Recommended v0.2.37 handoff. |
| Run E05 reuse-depth live generation comparison | deferred | Separate experiment stage. |
| Run E08 sidecar/passmode comparison | deferred | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | deferred | Separate Platform Harness stage. |
| Run actual E02 human panel | deferred | Requires human reviewer pool. |
| Add more E01 complex plan-first cases | deferred | Optional strategy evidence. |

## Linked Designs

- `docs/current-design/design_v0.2.37_e04_fixed_failing_draft_and_local_repair_v1.md`
- `docs/current-design/design_v0.2.37_e04_paid_full_rebuild_and_report_v1.md`

## Execution Plan

| Step | Status | Evidence |
| --- | --- | --- |
| Create fixed failing draft and failing mandatory test | completed | `scripts/e04_local_repair_vs_full_rebuild_experiment.py`; JSON evidence |
| Apply local repair and measure operations/time/test result | completed | local arm in JSON evidence |
| Run bounded paid Builder full rebuild for same requirement | completed | full rebuild arm in JSON evidence |
| Compare local repair vs full rebuild | completed | comparison in JSON and DOCX |
| Generate DOCX experiment report | completed | `docs/experiment-status/reports/2026-07-09_2021_E04_local_repair_vs_full_rebuild.docx` |
| Update experiment ledger | completed | `docs/experiment-status/v0.2_experiment_status.md` |
| Archive stage designs and workingon by version | pending | historical designs and stage report |

## Acceptance Criteria

- Both arms use the same requirement and same acceptance condition.
- Local repair starts from a real failing draft and uses real draft operations.
- Full rebuild uses configured paid model keys when available and records provider/model/usage.
- The report does not claim local repair is always better; it only answers the localized-failure case.
- Active `docs/current-design/` and `docs/workingon/` are empty except README after archive.

## Current Status

Experiment execution completed.

Evidence summary:

- local repair:
  - before test passed: `false`
  - after test passed: `true`
  - operation count: `1`
  - elapsed seconds: `0.0153`
- paid full rebuild:
  - build status: `published`
  - test report passed: `true`
  - elapsed seconds: `57.127`
  - model calls: `15`
  - tool calls: `22`
- conclusion: local repair is cheaper and sufficient for this localized template failure; this does not generalize to architecture-level failures.
