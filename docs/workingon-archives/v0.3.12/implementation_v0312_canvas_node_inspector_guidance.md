# implementation_v0312_canvas_node_inspector_guidance

Version: `v0.3.12`
Stage: `canvas_node_inspector_guidance`
Source stage report: `docs/stage-reports/v0.3.11_guided_try_run_recovery.md`

## Work Performed

- Added a node inspector guide for empty selection, edge selection, and node selection.
- Added selected-node plain-language summary before raw JSON config.
- Added safe edit guidance before direct JSON editing and deletion.
- Added bilingual inspector copy and CSS markers.
- Added `scripts/v03_12_canvas_node_inspector_guidance.py` and `tests/test_v03_12_canvas_node_inspector_guidance.py`.

## Verification

| Check | Result |
| --- | --- |
| Focused v0.3.12/v0.3.10 tests | pass, `10 passed` |
| Live v0.3.12 no-build smoke cleanup | pass |
| Combined v0.3.x regression and stage template tests | pass, `60 passed` |
| Diff whitespace check | pass |

## Evidence Summary

- Evidence file: `docs/workingon/canvas_node_inspector_guidance_v0.3.12.json`
- Source markers: node inspector, canvas selection, i18n, and style markers passed.
- Smoke app: created and cleaned.
- Cleanup counts: `builds=0`, `workflow_runs=0`.
- Forbidden build endpoint: not called.

## Outcome

v0.3.12 makes the concrete workflow edit surface less JSON-first and gives reviewers clearer selection-state guidance before they edit or delete workflow structure.
