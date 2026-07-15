# v0.4.3 Delivery Mode Policy

Status: completed

## Contract

- Task: `V04-03-T01B`
- Intents: `PRODUCT-003`, `PRODUCT-004`

## Problem

`ApplicationMode` currently distinguishes workflow from chat. Reusing it for Quick, Guided, and Governed would conflate interaction shape with assurance policy and make existing records ambiguous.

## Boundary

- Keep `ApplicationMode.workflow|chat` unchanged.
- Introduce a separate `DeliveryMode.quick|guided|governed` with a deterministic legacy default.
- A mode is a policy preset, not a capability claim and not an Evaluation Harness evidence level.
- Governed hard blocking requires an explicit policy flag; choosing the word Governed alone does not invent permissions or external evidence.

## Design

- Persist `delivery_mode` on applications and in `ApplicationSnapshot`; migrate missing database columns and old snapshot JSON to `guided`.
- Define one backend policy resolver returning publication behavior, recommended evidence, visible controls, and hard-gate state.
- Include the resolved policy in application detail and draft payloads so frontend copy does not reimplement policy.
- Add a segmented mode control in the creation flow and Studio settings with concise customer consequences.
- Preserve mode through draft mutation, version publication, restore, and application list/detail serialization.

## Acceptance

- New and legacy applications round-trip both application shape and delivery mode.
- Policy matrix tests cover all three modes and explicit Governed hard-gate on/off.
- Frontend exposes a stable accessible mode selector without changing workflow/chat semantics.

## Implementation Result

- `DeliveryMode.quick|guided|governed` is independent from `ApplicationMode.workflow|chat`.
- `resolve_delivery_policy` is the single backend policy matrix, including advisory, confirmation, and explicitly enabled Governed hard-gate semantics.
- SQLite initialization adds `delivery_mode='guided'` and `governed_hard_gate=0` idempotently for legacy databases.
- Application creation, draft metadata mutation, list/detail/draft responses, immutable versions, and restore all preserve delivery settings.
- Creation and Studio expose segmented mode controls; Studio exposes the hard-gate checkbox only for Governed mode.

## Verification Evidence

- `.venv/bin/python -m pytest tests/test_v04_03_delivery_modes.py -q` -> `5 passed, 1 warning`.
- `.venv/bin/python -m pytest tests/test_workflow.py -q` -> `77 passed, 1 warning`.
- Current v0.4.x gate -> `25 passed, 1 warning`.
- `npm run lint` could not execute because this host has no Node, npm, Docker, or alternate JavaScript runtime. This remains mandatory integrated evidence in `V04-03-T01F`; it is not recorded as a pass.
