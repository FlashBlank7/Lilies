# implementation_v0.2.53_adaptive_default_live_acceptance

## Goal

Close the accepted `v0.2.53` design set:

1. add a canonical `policy_default` runner arm,
2. run one real acceptance slice for omitted `reuse_depth`.

## Changes

- Added `policy_default` to the canonical E05 runner arm list and made its preflight/live path omit the `reuse_depth` query parameter.
- Extended E05 event extraction so template suggestion results now retain `reuse_depth_source`, `defaulted_by_policy`, `default_policy_version`, `available_overrides`, and a compact resolved strategy object.
- Added compact summary support for reuse-path reporting, so routine reads can see `policy_default -> adaptive -> deep` without reopening raw JSON.
- Ran a bounded paid/live acceptance on `data_analyzer` for `adaptive` and `policy_default`.
- Generated raw evidence, compact summary, and a concise DOCX report.

## Files

- `scripts/e05_template_reuse_depth_experiment.py`
- `scripts/summarize_experiment_evidence.py`
- `tests/test_e05_template_reuse_depth_experiment.py`
- `tests/test_summarize_experiment_evidence.py`
- `docs/experiment-status/evidence/experiment_v0.2.53_e05_data_analyzer_policy_default_live_2026_07_10.json`
- `docs/experiment-status/evidence/experiment_v0.2.53_e05_data_analyzer_policy_default_live_2026_07_10_summary.md`
- `docs/experiment-status/reports/2026-07-10_0720_E05_policy_default_live_acceptance.docx`

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| Runner regression | `13 passed, 1 warning` | `./.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py tests/test_summarize_experiment_evidence.py -q` |
| Paid/live acceptance | completed | `docs/experiment-status/evidence/experiment_v0.2.53_e05_data_analyzer_policy_default_live_2026_07_10.json` |
| DOCX structural QA | passed | `unzip -t docs/experiment-status/reports/2026-07-10_0720_E05_policy_default_live_acceptance.docx` |

## Live / Paid Acceptance

- Required: yes
- Provider/model: `multi / deepseek-v4-pro`
- Budget: `adaptive,policy_default`; `max_turns=42`; `max_repair_cycles=2`; `max_elapsed_seconds=600`; `provider_timeout_seconds=180`
- Command: `E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.53 ... ./.venv/bin/python scripts/e05_template_reuse_depth_experiment.py`
- Result:
  - `adaptive`: `published`, `379.655s`, `22/28`, `adaptive -> deep`, benchmark pass
  - `policy_default`: `needs_attention`, `186.714s`, `2/3`, `policy_default -> adaptive -> deep`, `model stream timed out after 180s`
- Skip reason: none

## Remaining Risk

- The omitted-depth default path is wired correctly, but its live reliability is not yet closed.
- The failure mode is not “wrong metadata”; it is provider timeout before mandatory test completion.
- This should become the next E05 engineering target instead of reopening fixed-depth policy selection.

## Design Decision

- Continue in next stage with default-path live reliability.
