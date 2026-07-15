# v0.4.4 Codex Scenario Catalog

Status: active

Tasks: `V04-04-T01B`, `V04-04-T01C`

## Decision

Add a reusable scenario catalog rather than another UI-only sample. The Codex entry owns a server-defined editable workflow, E2/E3 boundary, customer inputs, component evidence profile, claim scope, and generated acceptance cases.

## Workflow Shape

`Start -> Context -> Workspace -> Compact -> Capabilities -> Plan -> Budget -> Rounds -> Permission -> Sandbox -> Structured Loop -> Trace -> Answer`

The nested Loop is `Model Turn -> Tool Router -> branch -> Tool Executor/No-tool path -> Result Join -> Stop Controller -> Loop Output`. No legacy `claude_agent` and no implicit cyclic edge are allowed.

## Apply Contract

The scenario can replace a draft only through revision and content-hash guards. Workflow and generated tests commit atomically; stale or invalid requests leave the draft unchanged. The same server-defined template remains visible to Builder Team for model-led construction.
