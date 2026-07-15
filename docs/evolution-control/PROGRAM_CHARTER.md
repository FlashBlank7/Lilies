# Lilies Report Application Program Charter

## Charter Identity

| Field | Value |
| --- | --- |
| Charter ID | `LILIES-REPORT-APPLICATION-2026-07` |
| Charter version | `1.2` |
| Status | `frozen` |
| Source report | `docs/lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx` |
| Source instruction | User instructions on 2026-07-16 to absorb the report annotations, apply the complete report to Lilies, and make that campaign outrank local process or evidence gates |
| Task authority | Latest valid stage report only |

This charter is an intent constraint and completion contract. It is not a backlog and must not be used to select a next version directly.

## Campaign Objective And Priority

The campaign's highest-priority outcome is to implement and verify every intent extracted from the source capability-boundary report. Stage reports sequence that work; they do not compete with it.

Authority is ordered as follows:

1. the latest explicit user instruction;
2. this report-application campaign objective and `report_intents.json`;
3. the latest valid stage report's task sequencing;
4. the locked Stage Contract;
5. current-design expansion and working evidence.

A lower layer may constrain how a claim is verified, but it may not silently narrow the campaign, turn its own mechanism into the product goal, or indefinitely stop unrelated report intents.

## Product North Star

Lilies is a model-intelligence-first workflow construction and execution product. Builder Team reads block manuals, combines prior knowledge with customer requirements, and assembles reliable blocks, reusable workflow modules, runtime services, platform controls, and external connectors into usable workflows.

Engineering Harness mechanisms must improve delivery reliability without making ordinary workflow generation, editing, running, or publishing unnecessarily difficult.

## Product Invariants

1. `PRODUCT-001`: Model intelligence remains the primary source of requirement understanding, planning, composition, and repair.
2. `PRODUCT-002`: Workflow-internal Harness blocks, Evaluation Harness, and Platform Harness are separate concepts and may coexist.
3. `PRODUCT-003`: Quick, Guided, and Governed modes coexist. A new heavy path does not silently replace a simpler path before comparative evidence supports the change.
4. `PRODUCT-004`: In Quick and Guided modes, acceptance evidence informs the user's publishing decision but does not universally block publishing; Governed mode may enforce only an explicitly selected hard-policy boundary.
5. `PRODUCT-005`: Failed cases produce actionable repair guidance and can return workflow, node, trace, and failure context to Builder Team through a natural-language edit preview.
6. `PRODUCT-006`: Common configurable blocks expose human-readable controls. Raw JSON is an optional expert surface, not the default inspector.
7. `PRODUCT-007`: A behavior-affecting edit makes prior acceptance evidence stale and recommends revalidation without deleting the draft or hiding the user's publishing choice.
8. `PRODUCT-008`: Customer Runtime, Engineer Studio, and Governance Console have distinct information architectures.
9. `PRODUCT-009`: Reusable workflow modules and templates are first-class capability carriers.
10. `ARCH-003`: Capability placement is explicit: atomic block, module/subworkflow, runtime/platform service, platform hard control, or connector/external contract; not every missing capability becomes an atomic block.
11. `GOV-003`: Product claims never exceed available code, test, integration, live-run, telemetry, and evidence-registry support.

## Evolution Invariants

1. `EVOL-001`: The latest valid stage report is the only next-task authority; Workingon stores evidence only, and every stage task carries stable task and source-intent IDs.
2. `EVOL-002`: A Stage Contract is frozen before implementation, distinguishes mandatory from optional work, and cannot close while mandatory work is incomplete, blocked, deferred, weakened, or unsupported without a user-approved revision.
3. `EVOL-003`: Closure Audit reconstructs work from the frozen contract and evidence rather than trusting a summary; relevant tests and stale-evidence checks must run before a `pass` verdict.
4. `EVOL-004`: Implementation-route changes are allowed when objective and acceptance remain unchanged; goal, boundary, priority, or acceptance changes require the recorded authority defined below.
5. `EVOL-005`: Resume, context compaction, or a new session reloads this charter, the current Stage Contract, current task ID, and current evidence before planning.
6. `EVOL-006`: A version advances only for a coherent vertical capability, complete experiment, process-architecture repair, or explicit hotfix exception; repeated prerequisite-only versions are prohibited, and major-version closure includes the complete stage-report archive and unresolved-intent audit.
7. `EVOL-007`: Evidence availability and implementation completion are separate. An unavailable browser, provider, customer environment, or live integration produces a scoped evidence ceiling and debt record; it does not become a campaign blocker while another authorized report-intent route remains.
8. `EVOL-008`: A blocker is campaign-wide only when no remaining report intent can make meaningful progress, or when safety, irreversible action, or a genuine product decision requires user authority. Repeating an unchanged external probe is not progress.

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
3. Relevant focused, regression, integration, browser, live-model, or real-tool checks have run at the contracted closure floor. Unavailable higher evidence is recorded as `blocked_by_environment`, carries a claim ceiling and recheck trigger, and cannot support a broader claim.
4. Behavior-affecting changes invalidate stale evidence and rerun the applicable checks.
5. Closure Audit reports no missing mandatory task, weakened acceptance, unsupported claim, or disappearing intent ID.
6. The stage is large enough under its declared closure type.
7. The stage report passes the current template and evolution-control validators.

The contracted closure floor must be honest and useful. It may not claim product-level browser, live-model, real-tool, customer-environment, or production behavior without that evidence. A user-approved contract revision may convert an accidentally over-specified external evidence requirement into scoped evidence debt while preserving the implemented behavior and original report intent.

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
6. Continue the same task while it has an actionable implementation route. If only a higher external evidence level is unavailable, persist the claim ceiling and evidence debt, then continue the authorized report campaign instead of repeatedly probing or declaring the campaign blocked.

## Relationship To Codex Long-Running Work

The campaign follows the official long-running-work pattern: a clear outcome, explicit constraints, measurable definition of done, one continuous goal context when available, and durable repository state that survives interruption. Repository invariants belong in `AGENTS.md`; reusable execution instructions belong in a Skill; deterministic resume and closure checks belong in lifecycle hooks and scripts.
