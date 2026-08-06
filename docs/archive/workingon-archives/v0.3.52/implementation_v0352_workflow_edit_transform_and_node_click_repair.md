# v0.3.52 Workflow Edit Transform And Node Click Repair Implementation

## Summary

- Fixed a frontend crash class where clicking a canvas brick could switch into the edit panel and render workflow node fields with unsafe string assumptions.
- Added `safeText`, `safeWorkflowNodeType`, and `safeConfigKeys` guards to workflow summary, node inspector, reference chips, and run-step progress rendering.
- Extended deterministic workflow edit preview with `upsert_template_transform`.
- Natural-language instructions that mention output, summary, format, result, template, or transform can now insert or replace a `template_transform` before the terminal node and reconnect the graph.
- Unmatched workflow-edit instructions now fall back to an applicable workflow requirement update instead of returning `UNSUPPORTED`.
- Added an in-process FastAPI apply test proving preview operations can be applied to a real draft.
- Updated the current v0.3.x regression lane from 296 to 304 expected passes.

## Evidence

- `.venv/bin/python scripts/v03_52_workflow_edit_transform_and_node_click_repair.py`
- `.venv/bin/python -m pytest tests/test_v03_52_workflow_edit_transform_and_node_click_repair.py -q`
- `.venv/bin/python -m pytest tests/test_v03_47_natural_language_workflow_edit.py -q`
- `PATH="/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint`
- `.venv/bin/python -m pytest <current v0.3.x release gate from docs/testing/regression_lanes.json>`
- `git diff --check`

## Results

- v0.3.52 focused tests: `8 passed, 1 warning`.
- v0.3.47 workflow-edit regression: `7 passed`.
- Frontend TypeScript: passed.
- Current v0.3.x release gate: `304 passed, 1 warning`.
- Whitespace check: passed.

## Archived Workingon

- `workflow_edit_transform_and_node_click_repair_v0.3.52.json`
