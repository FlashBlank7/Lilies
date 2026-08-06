# Lilies Repository Instructions

## What this project is

A Dify-class visual AI workflow platform whose named Builder agent, Lilies,
turns natural-language requirements into runnable, editable, testable
workflows. The product authority is `docs/PRODUCT_NORTH_STAR.md`; the working
product description is `docs/BUSINESS_LOGIC.md`. Everything under
`docs/archive/` is history — read it for context, never for task sequencing.

## The one rule that outranks the rest

The platform exists to *generate workflows that work*. Prefer the change that
makes building, editing, running, or fixing a workflow simpler and more
observable. Do not add review gates, evidence ledgers, claim ceilings,
authority chains, or completion audits — that machinery was removed
deliberately in the `refactor/lean-core` campaign after it strangled the core
function. If a governance-shaped feature seems necessary, ask the user first.

## Minimum usable development

- This repository is maintained by one developer. Every task should produce a
  small, reviewable, working vertical slice.
- Finish and verify the core requested behavior before adding reuse,
  abstraction, hardening, packaging, or secondary interfaces.
- If implementation reveals adjacent work, ask whether it is strictly required
  for the current request. If not, leave it out.
- Prefer the least complex implementation that is genuinely usable. Add
  complexity only for a demonstrated failure or an explicit requirement.

## Ground rules

- `pytest` must stay green; run the affected test files before claiming done.
  Tests are behavior tests — do not add source-marker or evidence-audit tests.
- `MODEL_EGRESS_ENABLED=false` is the default; never enable real provider
  HTTP or spend tokens without explicit user authorization in this session.
- Preserve unrelated user changes in the dirty worktree. Never use
  destructive git commands to simplify a task.
- Frontend work targets a clean, product-style UI in plain user language —
  no internal jargon (claim ceilings, carriers, evidence levels) in copy.
- The local Lilies agent lives in `../LiliesAgent/` as a separate project.
  The platform-side bridge was removed; if re-integration is requested,
  design it against a thin versioned HTTP contract — no imports, no shared
  databases.
