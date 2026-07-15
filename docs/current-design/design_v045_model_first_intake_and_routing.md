# v0.4.5 Model-first Intake And Routing

Status: active

Tasks: `V04-05-T01B`, `V04-05-T01C`

## Decision

Keep the model responsible for interpreting the user's requirement and proposing F/G/X content. Option questions become typed decisions: each question names a decision axis and each option declares capability, envelope, or external-contract effects. The server validates and renders the model-produced contract into a stable workflow-building plan; it does not replace model reasoning with a fixed scenario template.

`ready` means a Capability Build Contract exists and passes structural closure. Legacy providers can be normalized into an explicitly marked compatibility contract, but new prompts and frontend paths use the structured contract directly.

## Routing

When a contract exists, Builder planning mode and runtime policy derive from its required envelope, risk, carrier choices, and unavailable external contracts. The old complexity router remains only as a compatibility fallback for requests without a contract. Routing output must identify its source and show envelope and risk separately.

## Customer Plan

The customer-facing workflow plan is rendered from the same contract sections: target user, business goal, start inputs, F/G/X closure, workflow steps, runtime interface, carrier choices, permissions, evidence plan, acceptance, claim scope, unresolved decisions, and next build suggestion.
