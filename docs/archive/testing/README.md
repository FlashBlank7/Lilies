# testing

This directory defines current and frozen regression lanes for automatic evolution.

## Authority

- Stage reports decide what the next version must do.
- `regression_lanes.json` defines machine-readable test lanes for the current product phase.
- `historical/v<version>_regression_lanes.json` freezes the lane contract used by archived evidence scripts. Historical scripts must never read the mutable current manifest.
- Historical evidence scripts default to ignored `.tmp/historical-evidence/<version>/` outputs. Re-running an old diagnostic must neither overwrite archived evidence nor place old-version files in active `workingon/`.
- `workingon/` evidence may record command output, but it never defines the next task or the release gate.

## Current Rule

- `v0.4.x_current_release_gate` is the gating lane for the active v0.4.x product stage.
- `full_historical_diagnostic` remains a full-suite diagnostic, but every non-pass must be classified.
- Archived-expectation conflicts are keyed by exact pytest node ID and applied as strict expected failures by `tests/conftest.py`; an unexpected pass is blocking until the manifest is reviewed.
- Current regressions, environment issues, unknown expected conflicts, and missing expected conflicts are blocking. `scripts/v04_03_regression_time_boundary.py` enforces this boundary from JUnit evidence.
- Expected test counts must match an observed command in the active stage report. Changing the count without changing or executing the command is not evidence.
