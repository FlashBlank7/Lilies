# implementation_v0.2.47_shallow_reuse_breadth_validation

## Goal

Close the accepted `v0.2.47` design set:

1. make E05 scoped reruns a canonical runner feature,
2. add a third E05 family (`data_analyzer`),
3. run paid/live breadth comparison and interpret the shallow/default policy signal.

## Completed So Far

### Design 1: runner scope controls

#### `scripts/e05_template_reuse_depth_experiment.py`

- Added `SELECTED_ARMS_ENV = "E05_REUSE_DEPTH_ONLY_ARMS"`.
- Added `selected_arm_depths(...)` and `selected_arms(...)`.
- Canonical runner now supports subset execution while preserving default full-arm behavior.
- Preflight and budget metadata now reflect the selected arm set.

#### `tests/test_e05_template_reuse_depth_experiment.py`

- Added coverage for:
  - default full-arm selection,
  - deep-only selection,
  - invalid arm filter rejection.

### Design 2: new `data_analyzer` family scaffold

- Added `data_analyzer_case()` to the E05 runner.
- Added `data_analyzer` aliases to `experiment_case(...)`.
- Added script coverage confirming the new family carries:
  - a distinct requirement,
  - `parameter_extractor` in required node types,
  - a valid benchmark reference with `template_transform`.

## Verification So Far

| Check | Result | Evidence |
| --- | --- | --- |
| E05 script tests after scope controls | `10 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` |
| E05 script tests after `data_analyzer` case | `11 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` |
| Final focused E05 regression before archive | `11 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` |

## Paid/Live Run

### Command

```bash
export E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.47
export E05_REUSE_DEPTH_CASE=data_analyzer
export E05_REUSE_DEPTH_RUN_ID=v0.2.47_e05_data_analyzer_breadth_20260710
export E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10.json
export E05_REUSE_DEPTH_MAX_TURNS=42
export E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2
export E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS=600
export E05_REUSE_DEPTH_TIMEOUT_SECONDS=900
export E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180
.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

### Outputs

- Raw JSON: `docs/experiment-status/evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10.json`
- Default summary: `docs/experiment-status/evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10_summary.md`
- DOCX report: `docs/experiment-status/reports/2026-07-10_0420_E05_data_analyzer_breadth_default_policy.docx`
- DOCX structural QA: `unzip -t docs/experiment-status/reports/2026-07-10_0420_E05_data_analyzer_breadth_default_policy.docx` -> passed
- DOCX visual QA: attempted with `render_docx.py`, blocked by missing `soffice`

### Arm Results

| Depth | Build status | Elapsed | Model/tool calls | Benchmark | Failure | Interpretation |
| --- | --- | --- | --- | --- | --- | --- |
| `none` | `needs_attention` | `395.359s` | `42 / 74` | case failed, score `0.71` | invalid draft / unreachable nodes | from-scratch path is unstable on this family |
| `shallow` | `needs_attention` | `600.247s` | `22 / 36` | case passed, score `0.85` | `BuildDeadlineExceeded` | structure is good enough, but build never converges inside the budget |
| `deep` | `published` | `461.068s` | `21 / 32` | case passed, score `0.85` | none | composed reuse is the only arm that closes this family cleanly |

## Stage Conclusion

`v0.2.47` completes both accepted designs and answers the carry-forward E05 question:

- the canonical runner now supports scoped reruns without ad hoc wrappers,
- `data_analyzer` gives a third paid/live family with clear architectural expectations,
- the new evidence weakens the fixed `shallow`-default hypothesis.

The important policy change is not “deep always wins.” It is narrower and more useful: a single fixed default reuse depth is no longer defensible across families. Lilies should move toward an adaptive policy that maps family/template signals to `none`, `shallow`, or `deep`.

## Next Action

Archive `v0.2.47`, then open the next stage around adaptive reuse-depth policy and validation.
