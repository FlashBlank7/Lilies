# v0.4.3 Schema-Driven Block Configuration

Status: active - implementation complete, browser evidence pending

## Contract

- Task: `V04-03-T01E`
- Intent: `PRODUCT-006`

## Problem

The block registry already exposes schemas and editor metadata, but common blocks still fall back to raw JSON in the Studio. Non-technical users cannot confidently configure model prompts, HTTP/tool calls, or Loop behavior, and a visual-only form would be misleading unless saved values reach canonical workflow runtime semantics.

## Boundary

- Cover model/model_turn, HTTP or tool, and Loop first.
- Generate controls from registry schema plus explicit editor hints; do not hard-code a second incompatible schema in React.
- Raw JSON remains available as an expert tab and round-trips unknown fields.
- Invalid values never silently coerce into a different runtime meaning.

## Design

- Normalize block `config_schema` into field descriptors for text, textarea, number, boolean, enum, string list, and JSON/object fallback.
- Render stable labeled controls with descriptions, required state, numeric bounds, and secret/reference treatment.
- Save through existing revision-checked draft mutation; validate server-side against the block registry before persistence.
- For Loop, expose condition, max iterations, break/cancel/checkpoint semantics represented by the current runtime contract.
- Add round-trip tests that configure each common family, reload the draft, and exercise a runtime outcome changed by the saved value.

## Acceptance

- Common blocks open in a human-readable form by default.
- Expert JSON and form views round-trip without data loss.
- Invalid schema values are actionable.
- Persistence and runtime tests prove the controls are functional, not decorative.

## Implemented Result

- The block registry now publishes typed editor fields and localized boundary notices for LLM, Model Turn, HTTP Request, Tool, Tool Executor, and Loop.
- The Studio derives a human-readable form from registry hints or the canonical JSON Schema, opens common blocks in form mode, and retains Expert JSON as a two-way view.
- Form saves clone the canonical config before applying typed paths, so unknown extension fields survive; required, enum, numeric, integer, JSON, and reference values fail visibly instead of silently coercing.
- Model Turn and Tool Executor received compatible server-side validators for their known settings while retaining extensible settings and routed Tool Executor semantics.
- Loop can persist a checkpoint after every iteration and emits `loop.checkpoint.saved`; the UI states the run-level cancellation and nested-workflow editing boundaries explicitly.

## Evidence

- `.venv/bin/python -m pytest -q tests/test_v04_03_schema_driven_block_config.py` -> `5 passed, 1 warning`.
- Current v0.4.x manifest gate -> `41 passed, 1 warning in 3.09s`; declared count is 41.
- `npm run lint` with verified local Node `v22.23.1` -> TypeScript pass.
- `npm run build` with the same Node toolchain -> Next.js `16.2.9` production build pass.
- Form-default, Expert JSON, and responsive interaction remain unverified because the supported Browser runtime exposes no browser; product acceptance stays open under `V04-03-T01F`.
