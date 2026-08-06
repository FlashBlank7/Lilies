# implementation_v0.2.55_e08_harness_sidecar_passmode

## Goal

Close the first runnable E08 comparison slice: compare workflow-internal soft harness/passmode behavior with Platform Harness sidecar hard-boundary behavior.

## Changes

- Added deterministic E08 runner:
  - `workflow_internal_permission_pause`
  - `workflow_internal_permission_auto_approve`
  - `platform_sidecar_network_block`
- Added focused regression test for scenario classification and hard/soft boundary semantics.
- Generated raw JSON evidence, compact summary, and DOCX report.

## Files

- `scripts/e08_harness_sidecar_passmode_experiment.py`
- `tests/test_e08_harness_sidecar_passmode_experiment.py`
- `docs/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10.json`
- `docs/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10_summary.md`
- `docs/experiment-status/reports/2026-07-10_0755_E08_harness_sidecar_passmode_comparison.docx`

## Verification

| Check | Result | Command |
| --- | --- | --- |
| Focused E08 runner regression | `1 passed, 1 warning` | `./.venv/bin/python -m pytest tests/test_e08_harness_sidecar_passmode_experiment.py -q` |
| Deterministic runner | completed | `./.venv/bin/python scripts/e08_harness_sidecar_passmode_experiment.py` |
| DOCX ZIP structural QA | passed | `unzip -t docs/experiment-status/reports/2026-07-10_0755_E08_harness_sidecar_passmode_comparison.docx` |
| DOCX render/PNG QA | skipped | `render_docx.py` failed because `soffice` is not installed on this machine. |

## Experiment Result

| Scenario | Layer | Passmode | Status | Enforcement | Bypassable |
| --- | --- | --- | --- | --- | --- |
| `workflow_internal_permission_pause` | workflow internal | `always_ask` | `paused` | `soft_pause` | yes |
| `workflow_internal_permission_auto_approve` | workflow internal | `auto_approve` | `succeeded` | `soft_pass` | yes |
| `platform_sidecar_network_block` | Platform Harness sidecar | `platform_policy_none` | `failed` | `hard_block` | no |

## Conclusion

Workflow-internal passmode can pause or pass by workflow configuration; Platform Harness sidecar policy is a hard boundary that fails the run before the external action. They should cooperate, but passmode is not a substitute for sidecar/hard-boundary enforcement.

## Remaining Risk

- This is a first deterministic comparison slice, not a complete sidecar product implementation.
- Future slices can compare cancellation, budget, worker lease, and UI/API control behavior.
