# implementation_v0347_natural_language_workflow_edit

## Summary

v0.3.47 turns the old narrow "natural-language draft edit" affordance into a workflow-level editing surface.

The slice addresses the user's product correction directly:

- The customer-facing name is now "自然语言工作流编辑" / "Natural-language workflow edit".
- The edit UI is a whole-workflow dialog, not a per-brick trick.
- Users can right-click or canvas-select bricks as references.
- References are sent to preview as context only and do not constrain the edit scope.
- The edit tab now starts with a readable workflow summary so a non-technical user can understand purpose and step sequence without opening every brick.
- The deterministic previewer supports broader workflow-level intents instead of returning unsupported for every non-rename instruction.

## Implementation

| Area | Change |
| --- | --- |
| Backend preview | Added `reference_node_ids`, workflow metadata updates, requirement updates, Start input updates, and workflow-scope fallback. |
| API | Preview endpoint passes `body.reference_node_ids` into `DraftPatchPreviewer.preview(...)`. |
| Frontend state | Added `workflowEditReferenceIds`, selected/reference node helpers, and preview request payload with references. |
| Canvas interaction | Added node context-menu reference capture and drag-selection reference capture via ReactFlow selection props. |
| Customer readability | Added natural-language workflow summary with purpose and step list before advanced node inspector. |
| Copy/i18n | Reframed zh/en UI from draft patching to workflow editing. |
| Style | Added workflow summary, edit dialog, and reference chip styling. |
| Release gate | Added `scripts/v03_47_natural_language_workflow_edit.py`, `tests/test_v03_47_natural_language_workflow_edit.py`, and updated `docs/testing/regression_lanes.json` to `263` expected passes. |

## Verification

| Check | Result |
| --- | --- |
| v0.3.47 focused tests | `7 passed` |
| v0.3.47 evidence script | `passed` |
| Existing preview non-destructive test | `1 passed, 1 warning` |
| Current v0.3.x release gate | `263 passed, 1 warning` |
| Live `/health` evidence | `passed`; read-only `GET /health` only |

## Evidence

- `docs/workingon-archives/v0.3.47/natural_language_workflow_edit_v0.3.47.json`
- `tests/test_v03_47_natural_language_workflow_edit.py`
- `scripts/v03_47_natural_language_workflow_edit.py`

## Handoff

The next stage should implement the customer-facing run interface that the user described: plain-language workflow description, simple user input/start, visible step progress, data-flow/progress state, and final result presentation without forcing the user into the brick canvas.
