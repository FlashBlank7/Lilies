# v0.4.3 Acceptance Repair Context

Status: active

## Contract

- Task: `V04-03-T01D`
- Intent: `PRODUCT-005`

## Problem

Acceptance repair preview exists, but the complete user promise is larger: a failed case must explain what failed, carry enough workflow/node/trace context into Builder Team, produce an inspectable edit preview, apply it once, and expose unsupported or failed repair honestly.

## Boundary

- Repair is a draft edit preview, never an automatic hidden mutation.
- References guide Builder Team but do not restrict the model to changing only those nodes.
- Applying a repair changes the draft hash and makes old evidence stale.
- Model/provider failure and unsupported deterministic fallback remain visible and retryable.

## Design

- Normalize each failed case into a repair context containing test ID, requirement, failed assertions, required blocks/tools, run ID, trace excerpts, relevant node IDs, and current content hash.
- Generate a concise natural-language repair instruction from structured failure data and allow the user to edit it.
- Route preview through the existing whole-workflow edit service and return operations, rationale, support state, and referenced context.
- Require expected revision and content hash on apply; return the new revision/hash and stale-evidence state.
- Reuse the Markdown renderer for repair rationale and failure details; keep engineering JSON behind a disclosure.

## Acceptance

- One failed case can preview and apply a repair end to end.
- Context fields are asserted in backend tests, not only UI marker tests.
- Stale revision/hash is rejected, unsupported preview is explicit, and retry leaves the draft unchanged.

