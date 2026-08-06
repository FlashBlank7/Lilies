# work_v0.2.49_adaptive_policy_live_validation

## Goal

Close the first live-validation slice after `v0.2.48`: canonicalize the `adaptive` arm inside the E05 runner and run a bounded paid/live comparison that shows whether adaptive materially improves over the fixed `shallow` candidate on a selected family.

## Source

- Stage report: `docs/stage-report-archives/v0.2.x/v0.2.48_adaptive_reuse_depth_policy.md`
- Version: `v0.2.49`

## Full Task Set

| Task | Disposition | Design | Reason |
| --- | --- | --- | --- |
| Add an `adaptive` arm to the canonical E05 runner | accepted | `design_e05_adaptive_runner_arm.md` | The next live slice should use the canonical runner instead of another wrapper. |
| Run bounded paid/live adaptive-vs-fixed validation on a selected family | accepted | `design_e05_adaptive_live_validation_case.md` | The policy now exists in backend code; the next question is live Builder behavior and cost. |
| Add Builder/API visibility for build deadline settings | deferred | none | Important, but independent from the narrow E05 adaptive validation slice. |
| Keep E08 sidecar/passmode comparison as a separate lane | deferred | none | The Harness architecture question deserves its own experiment stage. |

## Execution Status

| Design | Status | Evidence | Next action |
| --- | --- | --- | --- |
| `design_e05_adaptive_runner_arm.md` | completed | `scripts/e05_template_reuse_depth_experiment.py`; `tests/test_e05_template_reuse_depth_experiment.py`; `implementation_v0.2.49_adaptive_policy_live_validation.md` | proceed to live validation design |
| `design_e05_adaptive_live_validation_case.md` | completed | `docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10.json`; `docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10_summary.md`; `docs/experiment-status/reports/2026-07-10_0452_E05_adaptive_live_validation_data_analyzer.docx`; `implementation_v0.2.49_adaptive_policy_live_validation.md` | update stage report and E05 ledger |

## Acceptance

- All tasks dispositioned: yes
- All accepted designs completed/blocked/deferred: yes
- Verification: deterministic tests passed; bounded paid/live result recorded
- Experiment status updated: pending
- Archive ready: yes after stage report + historical design archive
