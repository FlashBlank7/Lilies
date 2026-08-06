# v0.3.51 Canvas Layout And Keyboard Navigation Implementation

## Summary

- Added a canvas-level `Arrange canvas` action that computes a deterministic left-to-right workflow layout from valid edges.
- Persisted arranged node positions through `update_node` draft mutations so cleanup survives refresh.
- Added focused-canvas WASD viewport panning with Shift acceleration and Alt/Option fine movement.
- Guarded keyboard panning so text fields, buttons, links, JSON editors, and workflow-edit inputs keep normal typing behavior.
- Updated the current v0.3.x regression lane from 288 to 296 expected passes.
- Repaired the v0.3.15 regression-lane guard so it validates a growing v0.3.x manifest instead of pinning the manifest top-level version to `v0.3.15`.

## Evidence

- `.venv/bin/python scripts/v03_51_canvas_layout_keyboard_navigation.py`
- `.venv/bin/python -m pytest tests/test_v03_51_canvas_layout_keyboard_navigation.py -q`
- `.venv/bin/python -m pytest tests/test_v03_15_regression_suite_lane_guard.py -q`
- `PATH="/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint`
- `.venv/bin/python -m pytest <current v0.3.x release gate from docs/testing/regression_lanes.json>`

## Results

- v0.3.51 focused tests: `8 passed`.
- v0.3.15 guard tests after repair: `5 passed`.
- Frontend TypeScript: passed.
- Current v0.3.x release gate: `296 passed, 1 warning`.

## Archived Workingon

- `canvas_layout_keyboard_navigation_v0.3.51.json`
- `regression_suite_lane_guard_v0.3.15.json`
