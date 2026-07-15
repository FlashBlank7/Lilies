---
name: lilies-evolution-development
description: Execute Lilies report-driven development workflows. Use in the Lilies repo for backend/platform/agent/workflow architecture tasks, stage or phase planning, workingon/current-design implementation, experiment evidence, rapid docs-evolution status/result reports, archive and rollback flows, automatic evolution mode, historical design recycling, stage/phase reports, and intellectual-asset screening for BlockFlow, Template, Harness, Platform Harness, and task monitor boundary conclusions.
---

# Lilies Evolution Development

Use this skill to run Lilies work as an execution process, not passive documentation. A `current-design` file is an implementation contract for one accepted task; after writing it, implement and verify it unless the user explicitly asks for design-only work.

## Campaign Objective Is Supreme

Treat completion of every intent in the capability-boundary report as the highest objective. Use this authority order:

1. latest explicit user instruction;
2. report campaign objective and `report_intents.json`;
3. latest valid stage report for sequencing;
4. Stage Contract;
5. current design and working evidence.

Never let a stage mechanism, test provider, archive rule, or repeated diagnostic become a competing mission. Stage reports still select the next task, but they must serve the campaign and cannot indefinitely freeze unrelated report work.

Separate implementation state from evidence level:

- Fix implementation failures inside the current task.
- When browser, model, tool, customer environment, or another external evidence surface is unavailable, record `blocked_by_environment`, achieved level, claim ceiling, and recheck trigger.
- Do not claim the unavailable level passed.
- Do not retry the same unchanged external condition on later turns. Continue an authorized report-intent route.
- Treat the whole campaign as blocked only when no remaining intent has a valid implementation, design, deterministic-test, or contract route, or when safety, irreversible action, or a genuine product decision requires user authority.
- If an earlier contract accidentally made an external evidence provider a campaign lock, use explicit user authority to revise it to truthful scoped closure and preserved evidence debt.

## Required Repo Context

Operate in `/Users/zhonghaoyang/Code/agent/Lilies` unless the user gives another Lilies checkout.

Before substantial work, read the smallest authoritative set first, in authority order:

1. `docs/evolution-control/PROGRAM_CHARTER.md`, especially Campaign Objective And Priority
2. `docs/evolution-control/report_intents.json`, especially `campaign_objective`
3. the current `docs/stage-reports/v*.md`, especially its locked `Stage Contract`, `Closure Audit`, and `Automatic Evolution Handoff`
4. `docs/PROJECT_EVOLUTION_STRATEGY.md`
5. `docs/README.md`
6. `docs/LANGUAGE_SYSTEM.md` if relevant
7. if active `docs/stage-reports/` has no current stage report, read the latest handoff under `docs/stage-report-archives/v<minor>.x/` plus the matching `docs/phase-reports/v<minor>.0_*.md`
8. relevant `docs/experiment-status/v*.md`, ledgers, and summary evidence
9. current-task `docs/current-design/*.md`, optional `docs/workingon/*.md`, and relevant `docs/historical-designs/*.md`

The charter and intent registry preserve the long-lived product intent. The current stage report supplies the finite task contract. A resume must continue its `Current task ID`; it must not reconstruct a new plan from the latest summary or workingon notes.

Do not read raw experiment JSON by default. Open `docs/experiment-status/evidence/*.json` only when a summary is disputed, missing a needed field, or the exact event trace is required.

Use `rg` / `rg --files` first. Do not move or rename historical documents unless the user explicitly asks for migration or archive cleanup.

## Reference Routing

Read only the reference files needed for the current task:

- `references/operating-gates.md`: automatic evolution, full task expansion, closure gates, design archive gate, continuation behavior.
- `references/archive-and-rollback.md`: archive, historical-design recycling, docs-only rollback, archive commits.
- `references/experiment-and-paid-models.md`: experiment governance, paid/live model verification, applied experiment markers, raw summary rules.
- `references/templates.md`: compact process templates for optional workingon evidence, current design, mandatory stage report, historical design, experiment ledger, rollback plan.
- `references/reasoning-budget.md`: how to allocate model effort and live/paid validation budget during long automatic evolution.

## Local Startup Protocol

When the user asks to start Lilies locally, do not guess backend ports from FastAPI defaults or prior runs.

1. Prefer `./scripts/dev_platform.sh`.
2. If manual startup is necessary, read `platform/frontend/.env.local` first, then `platform/frontend/.env.example`.
3. Treat `AGENT_PLATFORM_URL` as the Studio proxy target; current local development is normally `http://127.0.0.1:8001`.
4. Start Studio on `http://127.0.0.1:3000`.
5. Verify backend `/health`, Studio `/`, and Studio proxy `/api/platform/api/v1/applications`.
6. If proxy returns `500`, inspect the Next.js dev-server log before guessing. A common cause is `AGENT_PLATFORM_URL` pointing to a backend port that is not listening.

Docker Compose is different: API container listens on `8000`, and web uses `AGENT_PLATFORM_URL=http://api:8000` inside the Compose network.

## Request Classification

Use the full workflow when the task affects architecture, core backend behavior, Builder Team, BlockFlow, WorkflowSpec, Template, Harness, Platform Harness, task monitor boundary, testing gates, project reports, stage archive, or automatic evolution.

For a small one-file fix, do the fix directly and record only minimal evidence.

Enter **Automatic Evolution Mode** when the user says "自动演进", "项目自动演进", "continue next version", "complete and auto-archive", "完成后自动归档", or equivalent.

Use **Rapid Result Report** mode when the user asks what happened across versions, whether experiments/designs were completed, or wants a compact summary from docs-evolution artifacts.

## Core Workflow

1. Find the authoritative stage source.
   - Next-stage guidance belongs only in active `docs/stage-reports/`, or in the latest archived handoff stage report when a completed phase has been archived and the next phase has not started.
   - `current-design/` and `workingon/` are not roadmaps.
   - The latest stage report's `Next-stage Task Set` is the only source for the next version's task set unless the user gives a newer explicit instruction.
   - If the stage report is malformed, missing required sections, or unclear, repair the process/report first instead of inventing a task source.

2. Lock the stage contract from the stage report, not from workingon.
   - Read every task in the latest stage report's `Next-stage Task Set` and preserve its stable task ID and source intent IDs.
   - Classify tasks as mandatory or optional before implementation and record measurable acceptance criteria, required evidence, and surface/role in `Stage Contract`.
   - A mandatory task remains mandatory until completed. Only an explicit user-approved contract revision may remove or replace it; blocked or deferred mandatory work keeps the version open.
   - Optional work may be rejected, deferred, or superseded with evidence and an intent-preserving reason.
   - Record final disposition in the new stage report's `Source Task Set`, `Unresolved / Blocked / Deferred`, and `Intent Coverage` sections.
   - Do not put next-stage task decomposition, roadmap authority, or version selection into `workingon`.
   - Use `workingon/` only if intermediate evidence, experiment traces, implementation notes, or temporary investigation records are useful.

3. Expand accepted tasks into design contracts.
   - Create one or more `docs/current-design/design_<topic>.md` files for every accepted task that needs engineering, product, experiment, docs-process, or review work.
   - Each design must name its source stage report and source stage task.
   - A design contains problem, boundary, solution, implementation plan, acceptance criteria, and evidence requirements.
   - A design must not select the next version.

4. Implement designs one by one.
   - Do not stop after writing designs.
   - Implement the design in code or relevant project files.
   - Record meaningful implementation evidence in `docs/workingon/implementation_<topic>.md`.
   - If the design is wrong, revise the same design and continue.
   - Move to the next design only after implementation, verification, evidence, and remaining risk are recorded.

5. Verify at the claimed closure level.
   - Use deterministic tests for implementation correctness.
   - Use bounded paid/live model tests when behavior depends on model output, Builder quality, benchmark validity, workflow generation, tool execution, or Platform Harness enforcement.
   - Record provider/model, budget boundary, command, result, failure mode, and evidence path.
   - Distinguish the contracted closure floor from desired higher evidence. External unavailability limits claims and creates evidence debt; it is not implementation failure.

6. Run a closure audit against the locked contract.
   - Use a fresh-context read-only reviewer when available. The reviewer reads the Program Charter, current stage contract, diff, and evidence instead of trusting the implementation summary.
   - Every mandatory acceptance criterion must pass with valid evidence, every source intent must remain covered, and the version-size gate must pass.
   - A summary, commit, partial prerequisite, blocked mandatory task, or suggested next step is not closure.

7. Archive only when the closure audit and deterministic validators pass.
   - Normal mode: wait for the user to request archive.
   - Automatic Evolution Mode: archive, commit, and continue without a separate archive request.
   - Stage reports must use `docs/stage-reports/STAGE_REPORT_TEMPLATE.md` exactly, including explicit `none` rows when a section has no content.

8. When a major phase is complete, archive the stage-report set.
   - Create/update the phase report under `docs/phase-reports/`.
   - Move every completed `v0.<minor>.*` stage report out of active `docs/stage-reports/` and into `docs/stage-report-archives/v0.<minor>.x/`.
   - Add/update `docs/stage-report-archives/README.md`, `docs/stage-report-archives/v0.<minor>.x/README.md`, and `docs/stage-reports/README.md`.
   - Update `docs/README.md`, experiment-status links, and any stage-report references that would otherwise point at moved files.
   - Leave active `docs/stage-reports/` with only `README.md`, `STAGE_REPORT_TEMPLATE.md`, and current unarchived phase reports.
   - Record the archive range, count, latest handoff, next phase target, unresolved blockers, and verification result.

## Hard Gates

- Version evolution is serious. Do not leave a version because one useful slice is done.
- A version must close a serious version-sized unit. Before archiving, classify why the version is large enough:
  - multiple coordinated design contracts under one coherent stage goal,
  - a vertical/product slice across code surface, tests, docs, and user/operator visibility,
  - an experiment closure with report, evidence, ledger/index update, and application marker,
  - a P0 process/architecture repair touching the skill, templates, gates, and validation,
  - or an explicit emergency/hotfix/blocker exception with written justification.
- If a version would archive only one historical design, record why that is legitimate. If recent history repeatedly has one-design versions, treat it as a process smell and consolidate the next stage instead of advancing another tiny version.
- Every mandatory implementation task must be completed and pass its contracted acceptance criteria. Documentation cannot convert missing behavior into completion. A higher external evidence target may close only as explicit evidence debt under a user-approved scoped contract, and the unavailable level must remain unclaimed.
- Optional tasks may be rejected, deferred, or superseded with evidence, but their source intent must remain visible in `Intent Coverage` and a later stable task when still applicable.
- Every experiment must produce the required report artifact before it is called complete; for Lilies this normally means a concise `.docx` plus evidence.
- Never use an unfinished experiment as the basis for broad engineering improvement.
- If an experiment result changes engineering, mark it `已应用`; if it validates an existing change, mark it `验证应用`.
- Platform Harness slices must be named as slices unless enforcement, observability, persistence, cancel/timeout/budget behavior, UI/API visibility, tests, and operational behavior are all closed.
- Archive requires a passing `Closure Audit`, `scripts/validate_stage_report_template.py`, and `scripts/validate_evolution_control.py` in addition to task evidence.
- Archive must recycle current-stage designs into `docs/historical-designs/` and move active `workingon` material into versioned archives.
- After each small-version archive, active `docs/current-design/` and `docs/workingon/` should contain only README files unless a new active task is explicitly open.
- Every new stage report must pass the mandatory section contract in `docs/stage-reports/STAGE_REPORT_TEMPLATE.md`. Use `scripts/validate_stage_report_template.py` for new reports when possible.
- Major-version completion must include stage-report set archive. A phase is not fully archived if its completed `v0.<minor>.*` stage reports still sit in active `docs/stage-reports/`.
- Archive commits are automatic after a valid archive unless the user says not to commit.
- Docs rollback protocol is documentation-only. It must not roll back source code, database migrations, lockfiles, runtime artifacts, caches, or live state.

For detailed gates, read `references/operating-gates.md`, `references/archive-and-rollback.md`, and `references/experiment-and-paid-models.md`.

## Automatic Evolution Mode

Automatic Evolution Mode is a chain of finite, contract-locked stages: select the next version from the latest stage report's `Next-stage Task Set`, write source-linked designs, implement them, verify, run closure audit, archive with the mandatory stage-report template, commit, then resume from the next stable task ID. It continues until the user explicitly pauses/stops, the in-scope campaign reaches a terminal state, or a real blocker leaves no valid task route.

`Real blocker` means campaign-wide, not stage-local. Missing optional Browser infrastructure, a single unavailable provider, or a desired higher evidence level is not campaign-wide while other report intents can progress. Probe once, persist the evidence ceiling, and move on. Do not spend consecutive turns reconfirming unchanged infrastructure.

Automatic evolution is not permission to invent an unbounded mission. The Program Charter and intent registry preserve the campaign, while each stage report defines a bounded task set and definition of done. Use small implementation batches inside a serious version; do not create a new tiny version merely to report progress.

If a phase closeout is selected, complete the phase archive before stopping or starting the next major version: phase report, stage-report set archive, archive README files, docs index updates, and skill/process gate updates when a process gap caused the issue.

Do not final-answer in this mode merely because a stage was committed or a summary contains a suggested next step. Resume the `Current task ID`; after a valid archive, continue from the next stable task ID already authorized by `Next-stage Task Set`. If a decision task is explicitly present there, execute that bounded decision task without inventing a separate roadmap.

A final answer is allowed only when:

- the user explicitly pauses/stops or asks to stop after the current version,
- every in-scope campaign intent has one of the allowed terminal statuses: `implemented_verified`, `experiment_rejected`, `superseded_preserved`, or `user_rejected`,
- a real campaign blocker exists: every authorized report-intent route is exhausted by the same missing dependency, unbounded cost, destructive action needing consent, safety/privacy/legal risk, unrecoverable merge conflict, or absent/contradictory campaign authority.

Before each final answer in this mode, ask: "Does the active stage have a current stable task ID, an unfinished mandatory contract item, or an authorized next stable task ID?" If yes and the user did not ask to pause, keep going. Never derive this answer from `workingon`.

Read `references/operating-gates.md` before using this mode.

## Rapid Result Reports

When producing a quick report:

1. Define the source question, such as "v0.2.0 initial experiment backlog".
2. Read newest authoritative summaries first: `stage-reports`, `experiment-status` index, ledgers, summary evidence, `historical-designs/README.md`, and `git log --oneline`.
3. Read raw workingon archives or raw JSON only for disputes.
4. Classify items as: `completed experiment`, `implemented capability`, `deterministic verified`, `partially addressed`, `deferred`, `not started`, or `promoted asset`.
5. Separate "experiment completed" from "feature implemented".
6. Output a compact verdict, table, evidence paths, gaps, and next action.

If a file artifact is requested, use `references/templates.md` "Rapid Result Report".

## Terms

Use Lilies language precisely:

- `BlockFlow`: Builder Team-created, testable block workflow deliverable backed by `WorkflowSpec`.
- `WorkflowSpec`: executable DAG structure.
- `Template`: verified reusable `WorkflowSpec` asset.
- `AgentSpec`: configuration executed by `AgentRuntime`.
- `Harness`: deterministic execution, constraint, observation, recovery, and validation mechanism.
- `Platform Harness`: hard external governance boundary.
- `soft harness block`: workflow-internal expression of a constraint, not an unavoidable platform boundary.

Do not use "Agent" to ambiguously mean `AgentSpec`, `BlockFlow`, and generic AI system in the same document.
