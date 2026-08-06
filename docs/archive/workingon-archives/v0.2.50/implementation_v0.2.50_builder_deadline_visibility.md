# implementation_v0.2.50_builder_deadline_visibility

## Goal

Close the accepted `v0.2.50` design set:

1. normalize Builder deadline fields in API responses,
2. expose deadline input and status in Studio,
3. verify both backend contract and frontend typing.

## Completed So Far

### Design 1: Builder deadline API contract

#### `platform/backend/src/agent_platform/api.py`

- Added a normalized deadline summary helper:
  - `deadline: { enabled, max_elapsed_seconds }`
- `create_build(...)` now echoes:
  - `max_elapsed_seconds`
  - `deadline`
- `list_application_builds(...)` and `get_build(...)` now annotate build payloads with the same normalized deadline block.

#### `tests/test_workflow.py`

- Extended the existing build watchdog test to assert:
  - create-build response deadline echo,
  - get-build deadline summary,
  - list-builds deadline summary.

### Design 2: Studio deadline surface

#### `platform/frontend/app/applications/[id]/page.tsx`

- Extended local `Build` typing with:
  - `max_elapsed_seconds`
  - `deadline`
- Added optional build-deadline input state and validation.
- `startBuild()` now sends `max_elapsed_seconds` when the operator provides it.
- Build status area now renders whether a deadline is enabled and the configured value.

#### `platform/frontend/lib/i18n.ts`

- Added bilingual copy for:
  - deadline label,
  - help text,
  - invalid input notice,
  - active/inactive deadline status line.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| Backend deadline visibility regression | `1 passed, 70 deselected, 1 warning` | `.venv/bin/python -m pytest tests/test_workflow.py -k "build_level_watchdog_records_harness_metadata" -q` |
| Frontend TypeScript check via `node_repl` | `ok=true, diagnostics=0` | `mcp__node_repl` type-check against `platform/frontend/tsconfig.json` with `incremental=false` override |

## Live / Paid Acceptance

- Required: no
- Provider/model:
- Budget:
- Command:
- Result:
- Skip reason: this stage is a product-surface slice over existing deadline behavior, not a model-quality experiment.

## Remaining Risk

- Studio now surfaces configured deadlines, but it does not yet show a live remaining-time countdown.
- The home-page quick-start build path still uses the default no-deadline behavior; this stage intentionally scoped the control to the detailed Studio build panel.

## Design Decision

- archive current version
