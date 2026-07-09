# implementation_v0.2.49_adaptive_policy_live_validation

## Goal

Close the accepted `v0.2.49` design set:

1. make `adaptive` a canonical E05 runner arm,
2. run one bounded paid/live adaptive validation slice,
3. update the E05 evidence chain with real results.

## Completed So Far

### Design 1: canonical adaptive runner arm

#### `scripts/e05_template_reuse_depth_experiment.py`

- Added `adaptive` to `depth_arms()`.
- Adaptive arm instruction now tells the Builder to:
  - call `template_suggestions` with `reuse_depth='adaptive'`,
  - read `effective_reuse_depth` and `policy_reason`,
  - concretize BuildPlan depth before draft mutations.
- Event summaries now persist:
  - requested `reuse_depth`,
  - `effective_reuse_depth`,
  - `policy_reason`.
- Arm results now expose `adaptive_resolution` for live-report consumption.

#### `tests/test_e05_template_reuse_depth_experiment.py`

- Updated arm/default-selection expectations for the canonical four-arm set.
- Added preflight coverage for:
  - `code_reviewer` adaptive -> `shallow`
  - `data_analyzer` adaptive -> `deep`
- Extended event-summary parsing coverage for adaptive suggestion metadata.

## Verification So Far

| Check | Result | Evidence |
| --- | --- | --- |
| E05 runner regression after adaptive arm support | `12 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` |

## Paid/Live Run

- Required: yes
- Provider/model: `multi / deepseek-v4-pro`
- Budget: bounded single-family `data_analyzer` slice with `shallow,deep,adaptive`, `max_turns=42`, `max_repair_cycles=2`, `max_elapsed_seconds=600`
- Command:

```bash
export E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.49
export E05_REUSE_DEPTH_CASE=data_analyzer
export E05_REUSE_DEPTH_RUN_ID=v0.2.49_e05_data_analyzer_adaptive_live_20260710
export E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10.json
export E05_REUSE_DEPTH_ONLY_ARMS=shallow,deep,adaptive
export E05_REUSE_DEPTH_MAX_TURNS=42
export E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2
export E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS=600
export E05_REUSE_DEPTH_TIMEOUT_SECONDS=900
export E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180
./.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

- Result:
  - raw JSON: `docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10.json`
  - summary: `docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10_summary.md`
  - DOCX: `docs/experiment-status/reports/2026-07-10_0452_E05_adaptive_live_validation_data_analyzer.docx`
  - `shallow`: `published`, `213.959s`, `9 / 20` model/tool calls, benchmark pass
  - `deep`: `published`, `301.290s`, `15 / 23` model/tool calls, benchmark pass
  - `adaptive`: `published`, `159.669s`, `11 / 17` model/tool calls, benchmark pass
  - adaptive resolved to `deep` with `policy_reason=adaptive:complex_blocks:parameter_extractor`
  - adaptive BuildPlan reuse depth was concretized to `deep` during the live run
- Skip reason:

## Remaining Risk

- This is a single-family live validation, so it strengthens the adaptive policy but does not globally close E05.
- `shallow` unexpectedly published on this rerun, which means adaptive's advantage here is primarily a cost/time improvement signal rather than a binary success rescue.
- DOCX structural QA passed, but visual render QA remains blocked locally because `soffice` is unavailable.

## Design Decision

- proceed to next design
