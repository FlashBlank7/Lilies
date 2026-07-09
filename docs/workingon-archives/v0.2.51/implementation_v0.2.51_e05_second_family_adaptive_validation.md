# implementation_v0.2.51_e05_second_family_adaptive_validation

## Goal

Close the accepted `v0.2.51` design set:

1. run a second-family adaptive E05 live validation on `code_review`,
2. define a reusable adaptive defaultization gate from multi-family live evidence.

## Changes

### Design 1: second-family adaptive live validation

- Re-ran deterministic E05 runner coverage before the paid/live slice:
  - `./.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q`
  - result: `12 passed, 1 warning`
- Ran a bounded paid/live `code_review` comparison with:
  - `shallow / deep / adaptive`
  - `max_turns=42`
  - `max_repair_cycles=2`
  - `max_elapsed_seconds=600`
  - `provider_timeout_seconds=180`
- Generated:
  - raw evidence JSON
  - summary Markdown
  - concise DOCX report

### Design 2: adaptive defaultization gate

- Converted the new `code_review` live result plus the earlier `data_analyzer` live result into an explicit gate for when adaptive is strong enough to become the default Builder suggestion mode.
- Recorded that gate in:
  - `docs/experiment-status/ledgers/E05_template_reuse.md`
  - `docs/experiment-status/v0.2_experiment_status.md`
  - `docs/intellectual-assets/asset_adaptive_reuse_defaultization_gate.md`
- Conclusion of the gate:
  - require one shallow-resolving family and one deep-resolving family,
  - both must be bounded paid/live,
  - both must stay benchmark-clean and not underperform the strongest fixed arm on final operational outcome,
  - current evidence passes this gate for “default suggestion mode”, but not for removing fixed-depth controls.

## Files

- `docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10.json`
- `docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10_summary.md`
- `docs/experiment-status/reports/2026-07-10_0530_E05_code_review_adaptive_live_validation.docx`
- `docs/experiment-status/ledgers/E05_template_reuse.md`
- `docs/experiment-status/v0.2_experiment_status.md`
- `docs/intellectual-assets/asset_adaptive_reuse_defaultization_gate.md`

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| Deterministic runner verification | `12 passed, 1 warning` | `./.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` |
| Paid/live experiment execution | completed | `docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10.json` |
| Evidence summary generation | completed | `docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10_summary.md` |
| DOCX structural QA | passed | `unzip -t docs/experiment-status/reports/2026-07-10_0530_E05_code_review_adaptive_live_validation.docx` |
| DOCX visual render QA | blocked locally: missing `soffice` | `render_docx.py` failed with `FileNotFoundError: soffice` |

## Live / Paid Acceptance

- Required: yes
- Provider/model: `deepseek-v4-pro`
- Budget: single `code_review` family, three arms, `42 turns / 2 repair cycles / 600s build deadline / 180s provider timeout`
- Command:
  - `E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.51`
  - `E05_REUSE_DEPTH_CASE=code_review`
  - `E05_REUSE_DEPTH_RUN_ID=v0.2.51_e05_code_review_adaptive_live_20260710`
  - `E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10.json`
  - `E05_REUSE_DEPTH_ONLY_ARMS=shallow,deep,adaptive`
  - `E05_REUSE_DEPTH_MAX_TURNS=42`
  - `E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2`
  - `E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS=600`
  - `E05_REUSE_DEPTH_TIMEOUT_SECONDS=900`
  - `E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180`
  - `./.venv/bin/python scripts/e05_template_reuse_depth_experiment.py`
- Result:
  - `shallow`: `ready`, `388.427s`, `42 / 45` model/tool calls, benchmark-clean
  - `deep`: `needs_attention`, `382.777s`, `44 / 70` model/tool calls, benchmark-clean but `build_error=builder stopped before mandatory tests passed`
  - `adaptive`: `published`, `313.696s`, `38 / 46` model/tool calls, resolved to `shallow`, benchmark-clean
- Skip reason:

## Remaining Risk

- The defaultization gate is now explicit and passed for Builder suggestion mode, but code-level productization has not landed yet.
- `customer_support_router` remains mixed governance evidence, so adaptive should stay overrideable and observable after productization.
- DOCX visual render QA is still blocked on this machine because `soffice` is unavailable.

## Design Decision

- archive current version
