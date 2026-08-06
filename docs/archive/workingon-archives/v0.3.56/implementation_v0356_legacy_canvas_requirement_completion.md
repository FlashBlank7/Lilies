# v0.3.56 Legacy Canvas And Requirement Completion

## Scope

- User-triggered task: diagnose the brick-click frontend crash, handle likely old-project compatibility, rerun from a clean local project set, and add customer-facing requirement completion.
- Closure: legacy draft nodes are guarded in the canvas render/selection path, the home intake can ask clarification questions and apply a workflow-aligned requirement draft, old local project data has been moved out of runtime state, and a fresh clean draft has passed acceptance.

## Completed Work

| Area | Result | Evidence |
| --- | --- | --- |
| Legacy node-click compatibility | `BrickNode`, canvas sync, arrange, context menu, drag persistence, and architecture summary now sanitize node type/title/description/position/config before use. | `platform/frontend/app/applications/[id]/page.tsx` |
| Requirement completion | Home intake now asks targeted customer questions for audience, inputs, outcome, acceptance, boundaries, and steps, then applies a workflow requirement draft back to the main input. | `platform/frontend/app/page.tsx`; `platform/frontend/lib/i18n.ts`; `platform/frontend/app/globals.css` |
| Claude Code reference boundary | Borrowed the `needs_input` / `plan_ready` interaction idea from `references/claude-code`, but did not copy ExitPlanMode or code-plan output format. | `scripts/v03_56_legacy_canvas_requirement_completion.py` |
| Old project reset | Archived the old local runtime `data` directory containing 19 applications and 7272 event files, then restarted with a clean data directory. | `docs/workingon-archives/v0.3.56/legacy_project_data_reset_v0.3.56.json` |
| Clean rerun | Created one clean v0.3.56 draft, verified app list count is 1, detail page returns HTTP 200, and acceptance passes 1/1. | `docs/workingon-archives/v0.3.56/legacy_project_data_reset_v0.3.56.json` |

## Verification

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest tests/test_v03_56_legacy_canvas_requirement_completion.py tests/test_v03_55_remove_japanese_learner_customer_group.py -q` | `8 passed` |
| `.venv/bin/python scripts/v03_56_legacy_canvas_requirement_completion.py --output docs/workingon-archives/v0.3.56/legacy_canvas_requirement_completion_v0.3.56.json` | `status=passed` |
| `PATH="/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint` from `platform/frontend` | passed |
| Current v0.3.x release gate command from `docs/testing/regression_lanes.json` | `323 passed, 1 warning` |
| Clean draft acceptance run | `1 passed, 0 failed` |

## Runtime Notes

- `./scripts/dev_platform.sh` is still blocked in this local environment because Docker is not installed.
- For this version's rerun, API and Studio were started directly on `127.0.0.1:8001` and `127.0.0.1:3000`.
- The old data was moved to `.tmp/legacy-project-data-v0.3.56-20260714122639/data` rather than irreversibly deleted, so the runtime is clean while evidence remains recoverable.
