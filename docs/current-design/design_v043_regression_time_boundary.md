# v0.4.3 Regression Time Boundary

Status: completed

## Contract

- Task: `V04-03-T01A`
- Intents: `PRODUCT-003`, `PRODUCT-004`, `PRODUCT-007`
- Authority: `docs/stage-reports/v0.4.3_usability_modes_evidence_and_regression_stabilization.md`

## Problem

The current full suite mixes three different questions: whether current product behavior works, whether an archived rollout decision still describes today's default, and whether a historical static evidence script can still find its old test inside the mutable current release gate. As a result, one current manifest edit produces more than one hundred failures and hides the smaller set of real Builder and worker regressions.

## Boundary

- Historical evidence remains preserved and executable where its behavioral promise is still intended.
- A historical rollout assertion that was deliberately superseded is not rewritten to pretend it predicted the future.
- Current release behavior must have current tests; an archived test cannot be the only evidence for a v0.4.x claim.
- Unknown full-suite failures are blocking. A skip or expected failure needs a stable classification and evidence path.

## Design

1. Add a frozen v0.3 regression-lane snapshot, route v0.3 evidence scripts to it instead of mutable current release metadata, and keep their default outputs outside active `workingon`.
2. Keep the v0.4.x gate as the only current release gate and make its expected count executable rather than hand-maintained guesswork.
3. Produce `docs/workingon/v0.4.3_full_suite_failure_inventory.json` with exact node ID, family, classification, current owner, rationale, and replacement evidence.
4. Classify deliberately superseded v0.2 rollout assertions as archived-expectation conflicts through explicit pytest metadata. Use strict expected-failure semantics so an unexpected pass forces review.
5. Fix current Builder, worker, runtime, frontend, and v0.4.x failures in code or current tests. Do not classify them as history merely because they are inconvenient.
6. Rerun the focused gate and full suite after every classification change; record deltas by family.

## Acceptance

- Historical scripts no longer look up `v0.3.x_current_release_gate` inside the mutable v0.4 manifest.
- Current gate execution and declared count agree.
- Every full-suite non-pass has a machine-readable classification.
- No unclassified failure remains when the stage closes.

## Implementation Result

- Historical v0.3 evidence scripts now read frozen `v0.3.55` or `v0.3.56` lane snapshots under `docs/testing/historical/`; their default JSON outputs go to ignored `.tmp/historical-evidence/v0.3.x/` rather than the active-stage workspace.
- `docs/testing/regression_lanes.json` is the current v0.4.3 contract and declares the executed 45-test gate.
- `tests/conftest.py` maps only the 17 exact archived node IDs to strict expected failures. An unexpected pass is therefore a review failure rather than a silent success.
- `scripts/v04_03_regression_time_boundary.py` converts JUnit into a machine-readable inventory and exits non-zero for current regressions, unknown expected conflicts, or missing expected conflicts.
- Nine current Builder and worker contract tests were repaired at their actual isolation or expectation boundary instead of being reclassified as history.

## Verification Evidence

- Entry inventory: `docs/workingon/v0.4.3_full_suite_failure_inventory_entry.json` records 133 current regressions and 17 archived-expectation conflicts from the 150-failure entry baseline.
- Final inventory: `docs/workingon/v0.4.3_full_suite_failure_inventory.json` records zero current blockers, zero unknown expected conflicts, and all 17 expected historical conflicts observed.
- Historical v0.3 evidence: `.venv/bin/python -m pytest tests/test_v03_*.py -q` -> `321 passed, 1 warning`; active `workingon` contains only v0.4.3 files afterward.
- Current v0.4.x gate: the command in `docs/testing/regression_lanes.json` -> `45 passed, 1 warning`.
- Final full suite: `.venv/bin/python -m pytest -q --tb=short --junitxml=/tmp/lilies-v043-task-f-final3.xml` -> `729 passed, 17 xfailed, 1 warning`; classifier covers 746 tests with no current, unknown, or missing failure.
