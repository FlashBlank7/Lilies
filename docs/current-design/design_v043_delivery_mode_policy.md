# v0.4.3 Delivery Mode Policy

Status: active

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

