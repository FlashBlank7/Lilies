# implementation_v0.2.48_adaptive_reuse_depth_policy

## Changes

- Added `platform/backend/src/agent_platform/template_strategy.py` as the shared scoring and adaptive policy layer for template suggestions.
- Wired `GET /api/v1/templates/suggestions` to accept `reuse_depth=adaptive` and return `effective_reuse_depth`, `recommended_action`, and `policy_reason`.
- Wired Builder `template_suggestions` to use the shared helper, support `adaptive`, and expose a top-level effective depth for coordinator decisions.
- Added a deterministic E05 adaptive-policy backtest script and summary generator.
- Produced a concise DOCX report for the deterministic backtest so the evidence chain stays consistent with the project experiment discipline.

## Files

- `platform/backend/src/agent_platform/template_strategy.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/builder.py`
- `scripts/e05_adaptive_reuse_policy_backtest.py`
- `tests/test_workflow.py`
- `tests/test_e05_adaptive_reuse_policy_backtest.py`
- `docs/experiment-status/evidence/experiment_v0.2.48_e05_adaptive_reuse_policy_backtest_2026_07_10.json`
- `docs/experiment-status/evidence/experiment_v0.2.48_e05_adaptive_reuse_policy_backtest_2026_07_10_summary.md`
- `docs/experiment-status/reports/2026-07-10_0434_E05_adaptive_reuse_policy_backtest.docx`

## Verification

- `./.venv/bin/python -m pytest tests/test_workflow.py -k "template_suggestions_include_reuse_depth_actions or builder_template_suggestions_adaptive_returns_effective_depth" -q`
  - result: `2 passed`
- `./.venv/bin/python -m pytest tests/test_e05_adaptive_reuse_policy_backtest.py -q`
  - result: `1 passed`
- `./.venv/bin/python scripts/e05_adaptive_reuse_policy_backtest.py`
  - result: generated deterministic JSON + Markdown evidence with `exact_matches=2`, `bounded_matches=1`, `mismatches=0`

## Live / Paid Acceptance

- Required: no
- Provider/model:
- Budget:
- Command:
- Result:
- Skip reason: `v0.2.48` closes the deterministic policy + backtest slice only. Fresh paid/live adaptive-vs-fixed validation is carried forward as the next E05 stage.

## Remaining Risk

- The first adaptive rule is intentionally narrow. It uses visible block hints from template metadata and does not yet learn from runtime outcomes.
- Customer-support evidence remains mixed across governance slices, so the current `shallow` recommendation is conservative rather than globally proven optimal.
- `TemplateStore.load_builtins()` currently skips the unrelated `drone_intervention_v1.json` robotics template because its metadata does not match the active `TemplateMeta` schema.
- DOCX structural QA passed, but visual render QA remains blocked locally because `soffice` is unavailable in this environment.

## Design Decision

- proceed to next design
