# v0.4.5 Capability Model And Closure

Status: active

Tasks: `V04-05-T01A`, `V04-05-T01C`

## Decision

Introduce one typed Capability Build Contract as the shared truth from requirement intake through Builder output. Functional capabilities (F), runtime guarantees (G), and external contracts (X) remain distinct collections with stable IDs, dependencies, exclusions, envelope requirements, environment availability, carrier decisions, platform coverage, evidence plans, and claim scope.

E0-E5 is cumulative execution context, not a replacement for capability or risk. Risk remains an orthogonal field. Deterministic closure computes the strongest required envelope, dependency order, missing dependencies, exclusion conflicts, unavailable external contracts, and unbound carrier decisions without turning an unavailable customer environment into a malformed workflow.

## Reference Closures

- Codex-like workspace agent: interactive tool feedback and approval at E2, optionally durable at E3.
- Daily web collection: timer, dedupe, retry/resume, provenance, storage, and access contracts at E3.
- Customer-system embedding: tenant identity, schema, writeback, compensation, audit, and deployment contracts at E4/E5.

The three contracts must differ materially in F, G, X, envelope, carriers, and evidence plan while retaining their original natural-language requirement.

## Persistence

The contract is part of the canonical application snapshot and therefore follows draft revisions, content hashes, publication versions, and Builder team state. Read/validate APIs expose closure without recomputing semantics from keywords.
