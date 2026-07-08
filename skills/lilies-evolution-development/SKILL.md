---
name: lilies-evolution-development
description: Execute Lilies report-driven development workflows. Use when working in the Lilies repo on complex backend/platform/agent/workflow tasks, project evolution strategy, stage or phase planning, splitting a request into workingon plans and current-design documents, implementing those designs in code one by one, recording experiments or implementation evidence, deciding whether to continue the current design or move to the next design, waiting for user review before archiving, archiving workingon on request, rolling docs only back to an earlier stage baseline, creating stage-reports or phase-reports, or screening intellectual-assets such as BlockFlow, Template, Harness, Platform Harness, and task monitor boundary conclusions.
---

# Lilies Evolution Development

Use this skill to run Lilies work as an execution process, not as passive documentation. A `current-design` file is not the deliverable; it is an implementation contract that must be executed in code unless the user explicitly asks for design-only work.

## Required Repo Context

Operate in `/Users/zhonghaoyang/Code/agent/Lilies` unless the user gives another Lilies checkout.

Before substantial work, read:

1. `docs/PROJECT_EVOLUTION_STRATEGY.md`
2. `docs/README.md`
3. `docs/LANGUAGE_SYSTEM.md` if it exists, otherwise search `docs` for `LANGUAGE_SYSTEM.md`
4. Relevant `docs/intellectual-assets/asset_*.md`
5. Relevant `docs/current-design/design_*.md` and `docs/workingon/work_*.md`

Use `rg` / `rg --files` first. Do not move or rename historical documents unless the user explicitly asks for migration or archive cleanup.

## Workflow

### 1. Classify the Request

Use the full workflow when the task affects architecture, core backend behavior, Builder Team, BlockFlow, WorkflowSpec, Template, Harness, Platform Harness, task monitor boundary, testing gates, project reports, or stage archive.

For a small one-file fix, do the fix directly and record only minimal evidence.

### 2. Split Themes into Working Plans

For complex work, create or update `docs/workingon/work_<topic>.md`.

The work file must contain:

- goal
- scope
- plan table
- linked current-design files
- acceptance criteria
- current status

Do not treat chat-only plans as sufficient for Lilies stage work.

### 3. Expand Plans into Current Design

For each plan that needs implementation or review, create or update `docs/current-design/design_<component-or-flow>.md`.

The design must specify:

- goal
- module boundary
- data flow or control flow
- implementation plan
- acceptance criteria
- referenced intellectual assets

If a plan is purely administrative, a current design may be unnecessary; record that decision in the work file.

### 4. Implement Designs One by One

After creating or updating current-design files, do not stop. Implement one current design at a time in the actual codebase or relevant project files.

During implementation:

- update the work file status as plans complete
- place meaningful experiment records in `docs/workingon/experiment_<topic>.md`
- place meaningful implementation evidence in `docs/workingon/implementation_<topic>.md`
- keep ordinary command output out of docs unless it is evidence for a decision
- run focused verification appropriate to the change
- if implementation reveals the current design is flawed, update that current-design file and continue the same design
- if implementation reveals a larger directional problem, update the active workingon plan before continuing
- if the current design is implemented and verified, move to the next current-design in the active plan

Do not broaden scope into unrelated refactors.

### 5. Decide Current Design vs Next Design

After each implementation slice, make an explicit decision in the active workingon file:

- continue current design: use when verification failed, code exposed a design gap, or the implementation is incomplete
- revise current design: use when the design assumption was wrong but the overall direction remains valid
- update workingon direction: use when the issue affects the plan, priority, scope, or sequence
- proceed to next design: use only when the current design has code changes, verification evidence, and remaining risk recorded

The implementation loop continues until all current-design files in the active plan are either implemented, intentionally deferred, or explicitly blocked.

### 6. Wait for User Review Before Archive

When all plans in the active work file are complete, do not auto-archive. Stop after reporting completed code changes, verification, workingon evidence, and remaining risk. Wait for the user to inspect the intermediate files and completion status.

Only archive when the user explicitly says to archive workingon or archive the current stage.

When archiving is requested, create:


`docs/stage-reports/YYYY-MM-DD_stage_<topic>.md`

The stage report must summarize:

- completed items
- evidence
- incomplete items
- next-stage tasks
- intellectual asset candidates

Also archive every relevant design contract from the completed stage into historical design records:

- create `docs/historical-designs/` if it does not exist
- copy or consolidate every implemented, revised, deferred, or rejected `docs/current-design/design_*.md` that belonged to the stage
- archive design files only after the corresponding stage/version state exists in a stage report or equivalent accepted version-state document
- if a user asks to archive design files before the corresponding version state exists, refuse the design archive first and ask to create or confirm the version state
- name historical designs with an explicit version or stage marker, for example `v0.2.3_design_platform_harness_task_monitor_v1.md`
- do not use dates as the primary historical design filename; dates may appear inside the file metadata, but the filename must be version/state based
- never overwrite an earlier historical design version; preserve design evolution across stages
- record source stage, original design path, final status, implementation evidence, verification, and remaining risk
- keep `docs/current-design/` as the active design workspace; clear or replace active designs only when the user explicitly requests cleanup or the next stage requires it

Do not delete `workingon` files during archive unless the user asks for cleanup.

After a valid archive finishes, automatically create a git commit for the archive unless the user explicitly says not to commit.

Archive commit rules:

- run `git status --short` before staging
- build an explicit archive path list; never use `git add .`, `git add -A`, or broad directory staging when unrelated changes exist
- stage only files created, moved, updated, or deleted by the archive operation
- include skill/template updates in the same commit only when they were part of the archive process requested by the user
- do not stage unrelated source code, unrelated docs, local runtime output, `.tmp/`, generated caches, lockfiles such as `uv.lock`, or user changes from other tasks
- verify the staged set with `git diff --cached --name-status` before committing
- if the staged set contains unrelated files, unstage them and fix the path list before committing
- use a version/state based commit message, for example `docs: archive v0.2.2 designs` or `docs: archive v0.2.3 workingon`
- if the archive is refused because the corresponding version/state does not exist, do not create an archive commit
- if git is unavailable, hooks fail, or the repository is in a conflicted state, report the blocker and leave the archive files uncommitted
- report the commit hash after a successful archive commit

### 7. Screen Intellectual Assets

Create or update `docs/intellectual-assets/asset_<stable-topic>.md` only when the conclusion:

- required complex experiments, long development, architecture evolution, multi-paper reading, or expensive debugging
- will support multiple future designs or stages
- has an evidence chain
- has an applicability boundary
- states misuse cases

Ordinary stage summaries, meeting notes, short bug fixes, and transient experiment logs are not intellectual assets.

### 8. Phase Reports

Create `docs/phase-reports/YYYY-MM-DD_phase_<theme>.md` only when multiple stage reports form a clear major-version storyline.

Phase reports summarize stage sequence, mainline evolution, architecture changes, residual risk, and the next major direction.

## Industrial Verification And Paid Model Tests

Lilies is intended to become an industrially useful agent/workflow platform, not an offline toy. When a change depends on model behavior, Builder Team quality, workflow generation quality, tool execution, live provider compatibility, benchmark validity, or Platform Harness enforcement, do not avoid real paid model/API tests merely to save small cost.

Default verification order for such changes:

1. run focused deterministic tests first, so obvious implementation errors are cheap to catch
2. run bounded live acceptance with the real configured provider/model when credentials are available
3. record provider, model, prompt/task, budget cap, commands, evidence, result, failure mode, and approximate cost if visible
4. route resource-consuming live work through the task monitor boundary or explicitly document why that is not yet possible
5. place live acceptance evidence in `docs/workingon/implementation_<topic>.md` or a focused workingon report

Only skip paid/live model tests when:

- credentials or required services are unavailable
- the user explicitly forbids paid calls for the task
- the expected cost is material or unbounded and needs user confirmation
- the test would create unacceptable safety, privacy, legal, or data-loss risk

If a paid/live test is skipped, record the concrete reason and the next command needed to run it. For completed experiments, follow the project experiment rule: produce a concise `.docx` experiment report with background, design, result, and conclusion.

## Docs-Only Rollback Protocol

Use this protocol only when the user asks to roll documentation back to an earlier phase or stage, remove docs/current-design or docs/workingon content, undo a documentation stage archive, or return docs to a named documentation baseline such as v0.2.1.

1. Confirm the rollback is documentation-only. This protocol must not be used to roll back source code, database migrations, dependency lockfiles, generated runtime artifacts, local caches, or live application state.
2. Identify the target documentation baseline before changing files. Prefer an explicit commit hash when available; otherwise use the relevant stage report and `git log -- docs skills` to infer the baseline.
3. Use file-level restoration and deletion. Do not use `git reset --hard`, do not rewrite history, and do not delete unrelated user changes.
4. Classify documentation files into three lists before mutating: delete, restore, keep. Delete only docs introduced after the target baseline and explicitly in rollback scope. Restore only docs whose content must match the target baseline. Keep unrelated untracked files such as `uv.lock`.
5. Preserve process source files that the user explicitly wants to keep. For this project, keep `skills/lilies-evolution-development/` unless the user explicitly asks to remove the skill itself.
6. After rollback, run structural checks for the affected docs directories, compare docs against the target baseline, validate the skill if it changed, and report remaining expected diffs.

## Templates

When writing new process documents, use `references/templates.md`.

## Terms

Use Lilies language precisely:

- `BlockFlow`: Builder Team-created, testable block workflow deliverable backed by `WorkflowSpec`
- `WorkflowSpec`: executable DAG structure
- `Template`: verified reusable `WorkflowSpec` asset
- `AgentSpec`: configuration executed by `AgentRuntime`
- `Harness`: deterministic execution, constraint, observation, recovery, and validation mechanism
- `Platform Harness`: hard external governance boundary
- soft harness block: workflow-internal expression of a constraint, not an unavoidable platform boundary

Do not use "Agent" to ambiguously mean `AgentSpec`, `BlockFlow`, and generic AI system in the same document.
