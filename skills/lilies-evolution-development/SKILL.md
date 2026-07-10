---
name: lilies-evolution-development
description: Execute Lilies report-driven development workflows. Use in the Lilies repo for backend/platform/agent/workflow architecture tasks, stage or phase planning, workingon/current-design implementation, experiment evidence, rapid docs-evolution status/result reports, archive and rollback flows, automatic evolution mode, historical design recycling, stage/phase reports, and intellectual-asset screening for BlockFlow, Template, Harness, Platform Harness, and task monitor boundary conclusions.
---

# Lilies Evolution Development

Use this skill to run Lilies work as an execution process, not passive documentation. A `current-design` file is an implementation contract for one accepted task; after writing it, implement and verify it unless the user explicitly asks for design-only work.

## Required Repo Context

Operate in `/Users/zhonghaoyang/Code/agent/Lilies` unless the user gives another Lilies checkout.

Before substantial work, read the smallest authoritative set first:

1. `docs/PROJECT_EVOLUTION_STRATEGY.md`
2. `docs/README.md`
3. `docs/LANGUAGE_SYSTEM.md` if relevant
4. latest relevant `docs/stage-reports/v*.md`, read previous 5 versions at most.
5. relevant `docs/experiment-status/v*.md`
6. relevant `docs/experiment-status/ledgers/*.md`
7. relevant `docs/experiment-status/evidence/*_summary.md`
8. relevant `docs/current-design/*.md`, `docs/workingon/*.md`, and `docs/historical-designs/*.md`

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
   - Next-stage guidance belongs only in `docs/stage-reports/`.
   - `current-design/` and `workingon/` are not roadmaps.
   - The latest stage report's `Next-stage Task Set` is the only source for the next version's task set unless the user gives a newer explicit instruction.
   - If the stage report is malformed, missing required sections, or unclear, repair the process/report first instead of inventing a task source.

2. Select the stage scope from the stage report, not from workingon.
   - Read every task in the latest stage report's `Next-stage Task Set`.
   - Decide which tasks are accepted into the current version and which are blocked, deferred, or superseded.
   - Record final disposition in the new stage report's `Source Task Set` and `Unresolved / Blocked / Deferred` sections.
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

6. Archive only when the version target is genuinely closed or explicitly dispositioned.
   - Normal mode: wait for the user to request archive.
   - Automatic Evolution Mode: archive, commit, and continue without a separate archive request.
   - Stage reports must use `docs/stage-reports/STAGE_REPORT_TEMPLATE.md` exactly, including explicit `none` rows when a section has no content.

## Hard Gates

- Version evolution is serious. Do not leave a version because one useful slice is done.
- A version must close a serious version-sized unit. Before archiving, classify why the version is large enough:
  - multiple coordinated design contracts under one coherent stage goal,
  - a vertical/product slice across code surface, tests, docs, and user/operator visibility,
  - an experiment closure with report, evidence, ledger/index update, and application marker,
  - a P0 process/architecture repair touching the skill, templates, gates, and validation,
  - or an explicit emergency/hotfix/blocker exception with written justification.
- If a version would archive only one historical design, record why that is legitimate. If recent history repeatedly has one-design versions, treat it as a process smell and consolidate the next stage instead of advancing another tiny version.
- Every accepted design must be completed, revised and completed, or explicitly blocked/deferred with evidence.
- Every experiment must produce the required report artifact before it is called complete; for Lilies this normally means a concise `.docx` plus evidence.
- Never use an unfinished experiment as the basis for broad engineering improvement.
- If an experiment result changes engineering, mark it `已应用`; if it validates an existing change, mark it `验证应用`.
- Platform Harness slices must be named as slices unless enforcement, observability, persistence, cancel/timeout/budget behavior, UI/API visibility, tests, and operational behavior are all closed.
- Archive must recycle current-stage designs into `docs/historical-designs/` and move active `workingon` material into versioned archives.
- After each small-version archive, active `docs/current-design/` and `docs/workingon/` should contain only README files unless a new active task is explicitly open.
- Every new stage report must pass the mandatory section contract in `docs/stage-reports/STAGE_REPORT_TEMPLATE.md`. Use `scripts/validate_stage_report_template.py` for new reports when possible.
- Archive commits are automatic after a valid archive unless the user says not to commit.
- Docs rollback protocol is documentation-only. It must not roll back source code, database migrations, lockfiles, runtime artifacts, caches, or live state.

For detailed gates, read `references/operating-gates.md`, `references/archive-and-rollback.md`, and `references/experiment-and-paid-models.md`.

## Automatic Evolution Mode

Automatic Evolution Mode is an execution loop: select the next version from the latest stage report's `Next-stage Task Set`, write source-linked designs, implement them, verify, update experiment status, archive with the mandatory stage-report template, commit, then inspect the new stage report and continue until the user explicitly says to pause or stop.

Do not final-answer in this mode merely because a stage was committed, a handoff did not preselect one implementation task, or the latest evidence says there is "no meaningful single next task". If the latest stage report contains lane-selection, phase-report, governance, cleanup, or other meta tasks, open the smallest next stage that resolves that decision and continue.

A final answer is allowed only when:

- the user explicitly pauses/stops or asks to stop after the current version,
- a real blocker exists: credentials/services missing, unbounded cost, destructive action needing consent, safety/privacy/legal risk, merge conflict, or no valid next-stage source.

Before each final answer in this mode, ask: "Could I name any safe next version and first workingon file from the latest stage report, including a selection or meta-planning stage?" If yes and the user did not ask to pause, keep going.

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
