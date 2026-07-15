# v0.4.5 Builder, Frontend, And Repair Contract

Status: active

Tasks: `V04-05-T01D`, `V04-05-T01E`

## Decision

Builder Team must inspect the authoritative Capability Build Contract before mutation, map BuildPlan modules to capability IDs, choose a carrier before adding blocks, and bind required capabilities to actual nodes, modules, platform services, hard controls, or external contracts. A required capability cannot disappear behind a summary, and one capability does not automatically imply one new atomic block.

Unavailable X contracts remain explicit scoped gaps with a claim ceiling. They do not masquerade as workflow graph failures and do not authorize a higher evidence claim.

## Product Integration

- The home intake shows typed option effects and the final F/G/X contract.
- Application creation stores the exact contract; Builder and draft APIs return it.
- Acceptance cases can reference capability IDs and evidence targets.
- Acceptance repair receives the failed capabilities, contract claim scope, and selected evidence boundary.
- Customer plan text and machine contract stay consistent because both originate from the same structured result.

## Acceptance

Deterministic provider tests cover option effects and a ready contract, API tests cover persistence and contract-derived routing, Builder tests cover inspect/bind/output behavior, and frontend static/build checks cover the actual customer path. Reference closure tests prove Codex, scheduled collection, and embedded-system requirements do not collapse into one generic plan.
