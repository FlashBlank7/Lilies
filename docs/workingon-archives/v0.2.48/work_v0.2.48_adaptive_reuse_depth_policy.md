# work_v0.2.48_adaptive_reuse_depth_policy

## Goal

Turn the `v0.2.47` E05 conclusion into a real backend capability: fixed `shallow` defaulting is no longer credible, so Lilies needs an adaptive reuse-depth policy that can recommend `none`, `shallow`, or `deep` from requirement and template signals.

## Source

- Stage report: `docs/stage-report-archives/v0.2.x/v0.2.47_shallow_reuse_breadth_validation.md`
- Version: `v0.2.48`

## Full Task Set

| Task | Disposition | Design | Reason |
| --- | --- | --- | --- |
| Add a shared adaptive template strategy helper and wire it into API + Builder `template_suggestions` | accepted | `design_adaptive_template_strategy_helper.md` | The experiment conclusion should become a product/backend capability, not stay as a report-only insight. |
| Add deterministic adaptive-policy tests and a backtest artifact over existing E05 evidence | accepted | `design_adaptive_reuse_policy_backtest.md` | We need a reproducible proof that the policy aligns with the best known family outcomes. |
| Run a fresh paid/live adaptive-vs-fixed comparison | deferred | none | Valuable, but the first closure step is to make the policy explicit and testable in backend code. |
| Add Builder/API visibility for `max_elapsed_seconds` | deferred | none | Still important, but outside the narrow adaptive-policy closure slice. |

## Execution Status

| Design | Status | Evidence | Next action |
| --- | --- | --- | --- |
| `design_adaptive_template_strategy_helper.md` | completed | `platform/backend/src/agent_platform/template_strategy.py`; `platform/backend/src/agent_platform/api.py`; `platform/backend/src/agent_platform/builder.py`; `tests/test_workflow.py`; `implementation_v0.2.48_adaptive_reuse_depth_policy.md` | move to archive after stage report is written |
| `design_adaptive_reuse_policy_backtest.md` | completed | `scripts/e05_adaptive_reuse_policy_backtest.py`; `tests/test_e05_adaptive_reuse_policy_backtest.py`; `docs/experiment-status/evidence/experiment_v0.2.48_e05_adaptive_reuse_policy_backtest_2026_07_10_summary.md`; `docs/experiment-status/reports/2026-07-10_0434_E05_adaptive_reuse_policy_backtest.docx` | update E05 ledger and experiment index |

## Acceptance

- All tasks dispositioned: yes
- All accepted designs completed/blocked/deferred: yes
- Verification: focused pytest passed; deterministic backtest artifact generated
- Stage report updated: pending
- Archive ready: yes after stage report + historical design archive
