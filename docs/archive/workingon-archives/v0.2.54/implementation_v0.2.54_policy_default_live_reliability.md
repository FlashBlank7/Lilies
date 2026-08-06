# implementation_v0.2.54_policy_default_live_reliability

## Goal

Close the policy-default live reliability gap carried from `v0.2.53`: make the omitted-depth path reliably move from suggestion metadata into concrete Builder execution.

## Changes

- Added a compact `execution_contract` for policy-default template suggestion payloads.
- Exposed the same contract at the Builder `template_suggestions` tool result top level, so the coordinator does not need to infer the next action from nested template metadata.
- Tightened Builder prompt language: `reuse_depth_source="policy_default"` must be treated as a resolved adaptive policy decision, immediately setting `BuildPlan.reuse_depth` to `effective_reuse_depth`.
- Tightened the E05 `policy_default` runner arm so live runs must set BuildPlan to the concrete depth before broad search or draft mutation.
- Extended E05 event summaries to retain `execution_contract`.

## Files

- `platform/backend/src/agent_platform/template_strategy.py`
- `platform/backend/src/agent_platform/builder.py`
- `scripts/e05_template_reuse_depth_experiment.py`
- `tests/test_e05_template_reuse_depth_experiment.py`
- `tests/test_workflow.py`
- `docs/experiment-status/evidence/experiment_v0.2.54_e05_data_analyzer_policy_default_reliability_2026_07_10.json`
- `docs/experiment-status/evidence/experiment_v0.2.54_e05_data_analyzer_policy_default_reliability_2026_07_10_summary.md`
- `docs/experiment-status/reports/2026-07-10_0749_E05_policy_default_reliability_closure.docx`

## Deterministic Verification

| Check | Result | Command |
| --- | --- | --- |
| Focused E05 runner, summary, and default suggestion regressions | `15 passed, 1 warning` | `./.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py tests/test_summarize_experiment_evidence.py tests/test_workflow.py::test_builder_template_suggestions_default_to_adaptive_when_omitted tests/test_workflow.py::test_template_suggestions_include_reuse_depth_actions -q` |

## Live / Paid Acceptance

- Required: yes, because this stage's closure target is live Builder reliability.
- Provider/model: `multi / deepseek-v4-pro`
- Budget: `case=data_analyzer`; `arm=policy_default`; `max_turns=42`; `max_repair_cycles=2`; `max_elapsed_seconds=600`; `provider_timeout_seconds=180`; single-arm only.
- Command: `E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.54 E05_REUSE_DEPTH_CASE=data_analyzer E05_REUSE_DEPTH_ONLY_ARMS=policy_default E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.54_e05_data_analyzer_policy_default_reliability_2026_07_10.json E05_REUSE_DEPTH_RUN_ID=v0_2_54_policy_default_reliability E05_REUSE_DEPTH_TIMEOUT_SECONDS=900 E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180 E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS=600 ./.venv/bin/python scripts/e05_template_reuse_depth_experiment.py`
- Result: `published`, `322.364s`, `24/31` model/tool calls.
- Evidence:
  - `build_plan_reuse_depth=deep`
  - `template_suggestions=1`
  - `build_plan=6`
  - `template_expand=1`
  - `test_run=4`
  - `draft_publish=1`
  - `benchmark case_passed=true`, `case_score=0.85`
  - no provider failure events, no model timeout events
- Skip reason: none.

## DOCX Report QA

| Check | Result | Evidence |
| --- | --- | --- |
| DOCX ZIP structural QA | passed | `unzip -t docs/experiment-status/reports/2026-07-10_0749_E05_policy_default_reliability_closure.docx` |
| Render/PNG visual QA | skipped | `render_docx.py` failed because `soffice` is not installed on this machine. |

## Remaining Risk

- This closes one bounded `data_analyzer` policy-default reliability slice, not long-term monitoring.
- E08 sidecar/passmode remains a separate Harness experiment lane.
- Fixed-depth overrides remain available and were not changed.
