# work_v0.2.47_shallow_reuse_breadth_validation

## Goal

Use one more E05 task family to answer the next real policy question after `v0.2.46`: is `shallow` currently the most stable default reuse-depth candidate, and should the experiment runner itself support scoped reruns instead of ad hoc one-off wrappers?

## Source

- Stage report: `docs/stage-reports/v0.2.46_deep_reuse_deadline_governance.md`
- Version: `v0.2.47`

## Full Task Set

| Task | Disposition | Design | Reason |
| --- | --- | --- | --- |
| Add scoped arm selection to the E05 runner (`none` / `shallow` / `deep`) | accepted | `design_e05_case_runner_scope_controls.md` | v0.2.46 needed an ad hoc deep-only wrapper; this should become a first-class experiment control. |
| Add one more E05 task family and run a breadth comparison focused on shallow/default stability | accepted | `design_e05_data_analyzer_breadth_case.md` | This is the stage's main product question. |
| Add Builder `max_elapsed_seconds` UI/API visibility | deferred | none | Product/backend slice, not required to answer the current E05 policy question. |
| Run E08 workflow-internal gate vs sidecar/passmode comparison | deferred | none | Separate Harness experiment stage. |

## Execution Status

| Design | Status | Evidence | Next action |
| --- | --- | --- | --- |
| `design_e05_case_runner_scope_controls.md` | completed | `docs/workingon/implementation_v0.2.47_shallow_reuse_breadth_validation.md` | none |
| `design_e05_data_analyzer_breadth_case.md` | completed | `docs/workingon/implementation_v0.2.47_shallow_reuse_breadth_validation.md`; `docs/experiment-status/evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10_summary.md` | none |

## Acceptance

- All tasks dispositioned: yes
- All accepted designs completed/blocked/deferred: yes
- Verification: completed
- Experiment status updated: completed
- Archive ready: yes

## Final Stage Answer

`v0.2.47` answers the carry-forward policy question with a negative result for fixed `shallow` defaulting. On the new `data_analyzer` family:

- `none` failed with an invalid draft / unreachable-node runtime path.
- `shallow` reached benchmark-clean structure but still hit `BuildDeadlineExceeded` at the 600s build boundary.
- `deep` published in `461.068s` with fewer calls than `shallow`.

This means E05 should move from “is shallow the stable default?” to “how should Lilies choose reuse depth adaptively by family, template signal, or complexity?”
