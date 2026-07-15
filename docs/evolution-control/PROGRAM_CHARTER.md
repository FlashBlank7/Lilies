# Lilies Report Application Program Charter

## Charter Identity

| Field | Value |
| --- | --- |
| Charter ID | `LILIES-REPORT-APPLICATION-2026-07` |
| Charter version | `1.0` |
| Status | `frozen` |
| Source report | `docs/lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx` |
| Source instruction | User instruction on 2026-07-16 to absorb the report annotations and apply the complete report to Lilies |
| Task authority | Latest valid stage report only |

This charter is an intent constraint and completion contract. It is not a backlog and must not be used to select a next version directly.

## Product North Star

Lilies is a model-intelligence-first workflow construction and execution product. Builder Team reads block manuals, combines prior knowledge with customer requirements, and assembles reliable blocks, reusable workflow modules, runtime services, platform controls, and external connectors into usable workflows.

Engineering Harness mechanisms must improve delivery reliability without making ordinary workflow generation, editing, running, or publishing unnecessarily difficult.

## Product Invariants

1. `PRODUCT-001`: Model intelligence remains the primary source of requirement understanding, planning, composition, and repair.
2. `PRODUCT-002`: Workflow-internal Harness blocks, Evaluation Harness, and Platform Harness are separate concepts and may coexist.
3. `PRODUCT-003`: Quick, Guided, and Governed modes coexist. A new heavy path does not silently replace a simpler path before comparative evidence supports the change.
4. `PRODUCT-004`: In Quick and Guided modes, acceptance evidence informs the user's publishing decision but does not universally block publishing.
5. `PRODUCT-005`: Governed mode may enforce non-bypassable policies only when the workflow or organization explicitly opts into the relevant risk boundary.
6. `PRODUCT-006`: Common configurable blocks expose human-readable controls. Raw JSON is an optional expert surface, not the default inspector.
7. `PRODUCT-007`: A behavior-affecting edit makes prior acceptance evidence stale and recommends revalidation without deleting the draft or hiding the user's publishing choice.
8. `PRODUCT-008`: Failed cases produce actionable repair guidance and can be returned to Builder Team through natural-language workflow editing with a preview before application.
9. `PRODUCT-009`: Customer Runtime, Engineer Studio, and Governance Console have distinct information architectures.
10. `PRODUCT-010`: Reusable workflow modules and templates are first-class capability carriers. Not every missing capability becomes an atomic block.
11. `PRODUCT-011`: Capability placement is explicit: atomic block, module/subworkflow, runtime/platform service, platform hard control, or connector/external contract.
12. `PRODUCT-012`: Product claims never exceed available code, test, integration, live-run, and telemetry evidence.

## Evolution Invariants

1. `EVOL-001`: The latest valid stage report is the only source of the next-stage task set.
2. `EVOL-002`: Workingon stores intermediate results and execution evidence only. It never selects or decomposes the next stage.
3. `EVOL-003`: Every stage task carries stable task IDs and source intent IDs from `report_intents.json` or a newer explicit user instruction.
4. `EVOL-004`: A Stage Contract is frozen before implementation and distinguishes mandatory from optional tasks.
5. `EVOL-005`: The agent cannot defer, remove, weaken, or relabel a mandatory task to close a version. A user-approved scope change or a replacement preserving the original acceptance criteria is required.
6. `EVOL-006`: `blocked`, `not_run`, `partial`, `deferred`, and `documented` do not mean `completed`.
7. `EVOL-007`: A version advances only after all mandatory tasks pass their acceptance criteria and the Closure Audit verdict is `pass`.
8. `EVOL-008`: A product version closes a coherent vertical capability across its required model/API/runtime/UI/test/evidence surfaces. Repeated one-design or prerequisite-only versions are prohibited.
9. `EVOL-009`: Implementation-route changes are allowed when objective and acceptance remain unchanged. Goal, boundary, priority, or acceptance changes require a recorded deviation and the authority defined below.
10. `EVOL-010`: Resume, context compaction, or a new session reloads this charter, the current Stage Contract, the current task ID, and current evidence before any new planning.
11. `EVOL-011`: The implementation summary is not completion proof. Closure Audit reconstructs required work from the Stage Contract and evidence.
12. `EVOL-012`: A major-version closeout includes phase report, stage-report set archive, index repair, unresolved-intent audit, and next-phase handoff.

## Deviation Authority

| Deviation class | Example | Agent authority | Required record |
| --- | --- | --- | --- |
| `D0 implementation` | Library, data structure, file layout, algorithm | Allowed when acceptance is preserved | Deviation Register entry |
| `D1 substitution` | External dependency unavailable; equivalent test double or connector substituted | Allowed only with unchanged claim scope and explicit evidence limitation | Deviation Register plus affected acceptance/evidence |
| `D2 scope` | Drop mandatory task, weaken acceptance, change target user, change product boundary, change phase priority | User decision required | User decision reference and revised Stage Contract |
| `D3 safety/irreversible` | Destructive migration, high-risk external side effect, unbounded paid use | User decision required before action | Approval and rollback/containment evidence |

## Stage Completion Contract

A stage is complete only when all conditions hold:

1. Every accepted mandatory task has status `completed` and points to evidence.
2. Every mandatory acceptance criterion has a deterministic result.
3. Relevant focused, regression, integration, browser, live-model, or real-tool checks have run; skipped checks state why and cannot support a broader claim.
4. Behavior-affecting changes invalidate stale evidence and rerun the applicable checks.
5. Closure Audit reports no missing mandatory task, weakened acceptance, unsupported claim, or disappearing intent ID.
6. The stage is large enough under its declared closure type.
7. The stage report passes the current template and evolution-control validators.

## Campaign Completion Contract

The report-application campaign is complete only when every entry in `report_intents.json` has a terminal disposition:

- `implemented_verified`: implemented and verified at the claimed closure level.
- `experiment_rejected`: a bounded experiment disproved the path and records the retained alternative.
- `superseded_preserved`: a replacement preserves the original acceptance criteria and links evidence.
- `user_rejected`: the user explicitly rejects the intent.

`documented`, `planned`, `partial`, `blocked`, `deferred`, and `unassessed` are non-terminal.

## Resume Protocol

On startup, resume, or context compaction:

1. Read this charter.
2. Read the latest valid stage report and its Stage Contract.
3. Load the current mandatory task ID and source intent IDs.
4. Inspect current-design and workingon evidence for that task.
5. Verify repository status and existing user changes.
6. Continue the same task. Do not generate a new next step while the current mandatory task remains open.

## Relationship To Codex Long-Running Work

The campaign follows the official long-running-work pattern: a clear outcome, explicit constraints, measurable definition of done, one continuous goal context when available, and durable repository state that survives interruption. Repository invariants belong in `AGENTS.md`; reusable execution instructions belong in a Skill; deterministic resume and closure checks belong in lifecycle hooks and scripts.
