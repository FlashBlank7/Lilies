# v0.4.3 Schema-Driven Block Configuration

Status: active

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

