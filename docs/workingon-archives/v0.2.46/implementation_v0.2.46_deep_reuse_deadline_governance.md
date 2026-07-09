# implementation_v0.2.46_deep_reuse_deadline_governance

## Goal

Close the accepted `v0.2.46` design set end to end:

1. land Builder teammate deadline/repair-budget governance,
2. prove the guard deterministically,
3. rerun paid/live customer-support `deep` E05 under the same budget family,
4. update experiment artifacts and stage closure evidence.

## Code Changes

### `platform/backend/src/agent_platform/builder.py`

- Added teammate-governance constants:
  - `TEAMMATE_MIN_REMAINING_SECONDS = 90.0`
  - `TEAMMATE_REPAIR_BUDGET_EXHAUSTED_REASON = "repair_budget_exhausted"`
- Threaded `build_started_at` and `max_elapsed_seconds` into teammate entry points.
- Added `_remaining_build_seconds(...)`, `_is_repair_budget_exhausted_message(...)`, and `_teammate_guard_reason(...)`.
- `spawn_teammate` / `send_message` now refuse new teammate work when:
  - the current draft revision has already exhausted repair budget, or
  - remaining build deadline is too low for a long-tail teammate branch.
- Teammate `_agent_loop` now stops immediately after teammate-side `test_run` returns `maximum repair cycles reached`, emits `team.teammate.stopped`, and returns bounded evidence to the coordinator instead of continuing long debug work.

### `tests/test_workflow.py`

- Added a teammate-specific provider fixture that reproduces repair-budget exhaustion.
- Added deterministic tests for:
  - repair-budget stop reason,
  - low remaining-deadline refusal,
  - teammate stop after repair-budget exhaustion without extra post-exhaustion turns.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| Focused teammate governance regression | `4 passed, 66 deselected, 1 warning` | `.venv/bin/python -m pytest tests/test_workflow.py -k "teammate_guard_reason or repair_budget_exhaustion or build_level_watchdog" -q` |
| E05 script regression | `7 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` |
| Full pytest regression | `111 passed, 1 warning` | `.venv/bin/python -m pytest -q` |
| Static compile | passed | `.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts` |

## Paid/Live Evidence

### Attempt A: full-suite rerun kept as intermediate evidence

- Command family: `scripts/e05_template_reuse_depth_experiment.py`
- Result file: `docs/experiment-status/evidence/experiment_v0.2.46_e05_customer_support_teammate_governance_2026_07_10.json`
- Summary: `docs/experiment-status/evidence/experiment_v0.2.46_e05_customer_support_teammate_governance_2026_07_10_summary.md`
- Outcome:
  - `none` arm completed `ready`
  - run was interrupted before stage closure because the accepted `v0.2.46` task was deep-governance closure, while the full-suite path widened into a separate shallow/full-suite stability question

### Attempt B: deep-only closure rerun

- Result file: `docs/experiment-status/evidence/experiment_v0.2.46_e05_customer_support_deep_only_teammate_governance_2026_07_10.json`
- Summary: `docs/experiment-status/evidence/experiment_v0.2.46_e05_customer_support_deep_only_teammate_governance_2026_07_10_summary.md`
- DOCX report: `docs/experiment-status/reports/2026-07-10_0302_E05_customer_support_deep_teammate_governance.docx`
- Outcome:
  - `deep` arm finished `ready`
  - elapsed: `482.221s`
  - usage: `34 model calls / 53 tool calls`
  - benchmark: `case passed`, `score=0.85`

## Before/After Decision Point

| Slice | Build | Elapsed | Calls | Benchmark | Interpretation |
| --- | --- | --- | --- | --- | --- |
| `v0.2.45` customer-support `deep` | `needs_attention` | `602.071s` | `37/56` | passed | deadline failure despite benchmark-clean draft |
| `v0.2.46` customer-support `deep` | `ready` | `482.221s` | `34/53` | passed | accepted fix closed the deep deadline slice |

## Report QA

| Artifact | Result | Notes |
| --- | --- | --- |
| DOCX creation | passed | `docs/experiment-status/reports/2026-07-10_0302_E05_customer_support_deep_teammate_governance.docx` created |
| Render QA | blocked by missing `soffice` | `render_docx.py` failed with `FileNotFoundError: soffice` |

## Closure

- Accepted designs completed: yes
- Engineering change verified: yes
- Paid/live deep closure evidence exists: yes
- Global E05 closed: no
- Recommended next stage: validate whether `shallow` is the stable default candidate on at least one more task family/template, and separate full-suite breadth from deep closure.
