---
name: lilies-evolution-development
description: Execute Lilies report-driven development workflows. Use in the Lilies repo for backend/platform/agent/workflow architecture tasks, stage or phase planning, workingon/current-design implementation, experiment evidence, rapid docs-evolution status/result reports, archive and rollback flows, automatic evolution mode, historical design recycling, stage/phase reports, and intellectual-asset screening for BlockFlow, Template, Harness, Platform Harness, and task monitor boundary conclusions.
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
5. Relevant `docs/experiment-status/*.md`
6. Relevant `docs/current-design/design_*.md` and `docs/workingon/work_*.md`

Use `rg` / `rg --files` first. Do not move or rename historical documents unless the user explicitly asks for migration or archive cleanup.

## Local Startup Protocol

When the user asks to start Lilies locally, do not guess backend ports from FastAPI defaults or prior runs.

Use this order:

1. Prefer `./scripts/dev_platform.sh` as the local development entrypoint.
2. If manual startup is necessary, read `platform/frontend/.env.local` first, then `platform/frontend/.env.example`, and treat `AGENT_PLATFORM_URL` as the Studio proxy target.
3. Start the backend on the exact host and port used by `AGENT_PLATFORM_URL`; for the current local development setup this is normally `http://127.0.0.1:8001`.
4. Start the Studio on `http://127.0.0.1:3000`.
5. Verify all three endpoints before reporting success:
   - backend `/health`
   - Studio `/`
   - Studio proxy `/api/platform/api/v1/applications`
6. If the proxy returns `500`, inspect the Next.js dev-server log before guessing. A common cause is `AGENT_PLATFORM_URL` pointing to a backend port that is not listening.

Docker Compose is different: the API container listens on port `8000` and the web container uses `AGENT_PLATFORM_URL=http://api:8000` inside the Compose network. Do not mix Docker Compose port assumptions with local Studio development.

## Rapid Evolution Result Reports

Use this protocol when the user asks what happened across prior versions, whether old experiments or designs were completed, or wants a quick result report from docs-evolution intermediate artifacts.

1. Define the source question precisely, for example "v0.2.0 initial experiment backlog" or "latest Platform Harness stages".
2. Read newest authoritative summaries first: `docs/stage-reports/`, `docs/experiment-status/`, `docs/historical-designs/README.md`, `docs/current-design/README.md`, and `git log --oneline`.
3. Then read working evidence only for disputed or detailed items: `docs/workingon/work_*.md`, `docs/workingon/implementation_*.md`, active `docs/workingon/experiment_*.md`, archived `docs/workingon-archives/`, and completed experiment reports/evidence under `docs/experiment-status/`.
4. Treat document layers by authority:
   - `stage-reports`: completed version state and archive decision
   - `historical-designs`: final design contract per version
   - `workingon`: intermediate evidence, experiment traces, unresolved questions
   - `current-design`: active or reference design contracts, not proof of completion by itself
   - `experiment-status`: current experiment closure, application markers, and backlog status
   - `intellectual-assets`: promoted reusable conclusions, not raw progress logs
5. Classify every item as one of: `completed experiment`, `implemented capability`, `deterministic verified`, `partially addressed`, `deferred`, `not started`, or `promoted asset`.
6. Separate "experiment completed" from "feature implemented". A paid/live run with a DOCX report is an experiment; deterministic tests and code changes are implementation evidence; a stage report is archive evidence.
7. Produce a compact report with:
   - one-line overall verdict
   - table or bullets for each original item
   - evidence paths
   - remaining gaps and next action
8. Prefer the latest stage report when it conflicts with older workingon notes, but mention the conflict if it affects the answer.
9. Do not create or archive files unless the user asks for a file artifact. If a file artifact is requested, use `references/templates.md` "Rapid Result Report".

## Workflow

### 1. Classify the Request

Use the full workflow when the task affects architecture, core backend behavior, Builder Team, BlockFlow, WorkflowSpec, Template, Harness, Platform Harness, task monitor boundary, testing gates, project reports, or stage archive.

For a small one-file fix, do the fix directly and record only minimal evidence.

If the user asks to "continue the next version", "complete and auto-archive", "automatic evolution", "自动演进", "继续下一个版本", "完成后自动归档", or the active objective has the same meaning, enter **Automatic Evolution Mode**.

### Stage Authority Model

Treat version evolution as a serious delivery process.

- `stage-reports/` is the only layer that may guide the next stage. Its `Next-stage Tasks` and handoff define what the next version must address.
- `current-design/` is not a roadmap and must never guide the next stage. A design file only expands one accepted task into a concrete implementation plan with acceptance criteria.
- `workingon/` is not a roadmap and must never guide the next stage. It only stores active execution evidence, intermediate results, command evidence, questions, and temporary observations.
- `intellectual-assets/` may justify designs, but it does not select the next version by itself.
- Do not pick one convenient next task from a stage report and ignore the rest. Convert the stage report's full next-task set into the next version scope, unless a task is explicitly rejected, blocked, deferred, or superseded with evidence.

### Automatic Evolution Mode

Automatic Evolution Mode is an execution loop for versioned Lilies development. It does not stop after making a design, finishing one implementation slice, or archiving one stage. It keeps selecting, planning, implementing, verifying, archiving, committing, and advancing to the next version until the user's objective is actually complete, the user explicitly pauses/stops, or a real safety/cost/external blocker prevents meaningful progress.

In this mode:

- start from the latest committed stage report and version marker, then choose the next version such as `v0.2.4`
- choose the next stage from the prior stage report's full `Next-stage Tasks` and `Automatic Evolution Handoff`, unless the user explicitly overrides it
- create/update `docs/workingon/work_<topic>.md` before implementation
- create/update `docs/current-design/design_<component-or-flow>.md` for every next-stage task that needs implementation or review
- complete the accepted design set one design at a time; do not move to a different version because one slice became available or inconvenient
- implement the design in code or relevant project files before moving on
- write implementation evidence in `docs/workingon/implementation_<topic>.md`
- run focused deterministic verification, and run bounded paid/live model tests when the change depends on model behavior, Builder quality, benchmark validity, workflow generation, or Platform Harness enforcement
- update the relevant `docs/experiment-status/` file whenever the stage creates, completes, applies, defers, blocks, or supersedes an experiment
- archive the completed stage without waiting for a separate user "archive" request
- before starting a new version, confirm the previous version's design files were archived into `docs/historical-designs/`; if not, repair that archive gap first
- copy every stage design to `docs/historical-designs/v<version>_design_<topic>.md` after the stage report exists
- during every small-version archive, recycle all current-stage designs into historical design records before the archive commit
- create a git commit automatically after each completed stage
- after a successful archive commit, immediately inspect the new stage report's next-stage task pool and continue the next version if the objective still calls for ongoing evolution

#### Minor Version Completion Gate

Run this gate before every small-version archive, especially in Automatic Evolution Mode. Do not leave the current version merely because a useful slice was implemented.

1. Re-read the active `docs/workingon/work_*.md`, the relevant `docs/current-design/design_*.md`, and any experiment backlog or stage goal that created the version.
2. Ask: "Did this version genuinely complete the target it claimed, or only complete a prerequisite?"
3. Run the Engineering Capability Closure Gate below for every engineering capability named in the version.
4. Check every design acceptance criterion and every experiment deliverable named in the active work. A design is not complete because code was written; it is complete only when implementation, verification, evidence, and remaining-risk notes exist.
5. If the version objective includes an experiment, do not mark the experiment complete without the required result artifact. For Lilies experiments, that normally means a concise `.docx` report plus raw evidence when applicable.
6. If the version uses an experiment result to improve engineering, confirm the experiment is completed, the result is sufficient, and the experiment status ledger marks it `applied` or equivalent. In Lilies Chinese docs use `已应用` or `验证应用`.
7. If only a prerequisite was completed, continue the current version with a smaller next slice unless one of the hard blockers applies.
8. If a blocker prevents finishing the original target, record it explicitly in `workingon`, `docs/experiment-status/`, and the stage report as `blocked`, `deferred`, or `carried forward`, with the next concrete command or design needed. Do not silently convert an unfinished experiment into a completed capability.
9. Only archive when all accepted current-version targets are completed or explicitly dispositioned with evidence.

Treat "useful progress" as insufficient for version completion. Version completion requires target closure.

#### Engineering Capability Closure Gate

Use this gate for every engineering task, not only experiments. Platform Harness, Builder benchmark, natural-language editing, Template RAG, UI surfaces, runtime policies, storage, scheduler behavior, and API changes must all close their real product/engineering objective before being called complete.

For each capability, classify the intended closure level before implementation:

- `backend slice`: backend logic/API only
- `vertical slice`: backend + frontend/API usage + tests
- `platform boundary`: enforcement, observability, persistence, cancel/timeout/budget behavior, and failure reporting
- `product capability`: user-facing workflow, UI, docs, tests, and operational evidence
- `research experiment`: experiment report plus evidence chain

Then verify the closure checklist that matches the claimed level:

1. Scope closure: the stage goal, workingon plan, and current design agree on what "done" means.
2. Code closure: every promised code path is implemented, including error paths and integration points.
3. Harness closure: resource use, cancellation, budget, timeout, persistence, audit, and recovery boundaries are handled or explicitly out of scope.
4. UI/API closure: if the capability is user-facing, the Studio/API surface is usable; backend-only is not enough unless the stage explicitly claims a backend slice.
5. Verification closure: focused tests, regression tests, and live/paid acceptance are run when relevant.
6. Evidence closure: workingon implementation evidence, stage report, historical design record, and any experiment-status update are present.
7. Gap closure: every known missing piece is marked `carried forward`, `deferred`, or `blocked` with a concrete next action. Do not bury major missing pieces in a vague "future work" line.

If a Platform Harness task only adds one policy or one backend guard, name it as a partial policy slice. Do not call it "Platform Harness complete" until the hard-boundary chain is closed across enforcement, observability, persistence, UI/API visibility, tests, and operational behavior.

#### Design Archive Gate

Run this gate in Automatic Evolution Mode and in normal document-evolution development before starting a new stage and again before committing a stage archive.

Before starting a new version:

1. Identify the latest completed stage report and its version, such as `v0.2.9`.
2. Check that every design referenced by that stage report, its workingon file, or its current-design links has a corresponding historical record in `docs/historical-designs/`.
3. If a previous-stage design is missing, repair the historical design archive first. Do not begin the next version until the previous version's design archive is complete or the stage report explicitly marks the design as rejected/deferred with evidence.

During each small-version archive:

- collect every `docs/current-design/design_*.md` created, revised, implemented, deferred, or rejected for the completed version
- write each historical design as `docs/historical-designs/v<version>_design_<topic>_v<n>.md`; do not use dates as the primary filename
- record source stage, original design path, final status, implementation evidence, verification evidence, remaining risk, and whether the active design should remain in `docs/current-design/`
- update `docs/current-design/README.md` and `docs/historical-designs/README.md` when their status inventory changes
- include a `Historical Designs` or equivalent section in the stage report so the next run can audit completion quickly
- refuse to archive or advance if the stage has no accepted version/state document yet

Treat design recycling as part of the archive, not as optional cleanup. A stage archive is incomplete until its design contracts have been recovered into historical design records.

#### Continuation Gate

After every archive commit in Automatic Evolution Mode, run this gate before any final answer:

1. Read the just-committed stage report's `Next-stage Tasks` and `Automatic Evolution Handoff`.
2. Run the Minor Version Completion Gate against the just-completed version. If the stage report reveals an original experiment/design target was only partially addressed, do not treat the objective as complete; choose the smallest continuation task.
3. Run the Design Archive Gate against the just-completed version and repair any missing historical design records before selecting the next version.
4. Run `git log --oneline -3` and `git status --short` to confirm the commit and unrelated working tree noise.
5. If the handoff says continue, or if any next-stage task is concrete and unblocked, do not send a final answer. Select the next version from the full next-task set, create/update its `workingon` and all required `current-design` files, and start implementation.
6. A final answer is allowed only when the user explicitly pauses/stops, no meaningful next task exists, or a real blocker from the hard-boundary list below is present.
7. If context pressure, long runtime, or fatigue is the only reason to stop, treat that as a process failure. Instead shrink the next stage, commit a small safe slice, and continue. Do not declare the objective complete.

Use this quick test before finalizing: "Could I name the next version and first workingon file from the latest stage report?" If yes, keep going.

Use this stricter test before starting a new version: "Have I expanded every accepted next-stage task into a design or an explicit blocked/deferred/superseded decision?" If no, do not begin implementation.

Automatic Evolution Mode still has hard boundaries:

- do not use `git reset --hard`
- do not stage unrelated files, generated caches, `.tmp/`, lockfiles such as `uv.lock`, or user changes outside the stage
- do not invent cosmetic versions just to keep moving; each version must be justified by a concrete task source
- do not make unbounded paid calls; use explicit budgets and record evidence
- stop only for explicit user pause/stop, irreversible/destructive action needing consent, missing credentials/services, material unbounded cost, safety/privacy/legal risk, merge conflicts, or the absence of any meaningful next version task

When Automatic Evolution Mode is active, replace the normal "wait for user review before archive" behavior with "archive and commit after verification, then advance".

#### Full Task Set Expansion Gate

Run this gate before implementation for every small-version stage.

1. Read the latest stage report's `Next-stage Tasks` and `Automatic Evolution Handoff`.
2. List every next-stage task in the active workingon file.
3. Disposition every task as exactly one of: `accepted`, `blocked`, `deferred`, or `superseded`.
4. Create one or more `docs/current-design/design_*.md` files for every accepted task that needs engineering, product, experiment, docs-process, or review work.
5. Do not implement only the easiest next-stage task while leaving the rest unclassified.
6. If the task set is too large for one version, split it explicitly in the workingon file and stage report with reasons. The split must still preserve the full task set and explain which tasks belong to the current version and which are carried forward.
7. Do not advance versions until every accepted design for the current version is completed, revised and completed, or explicitly blocked/deferred with evidence.

This gate is what prevents version evolution from degenerating into one-design-per-version drift.

#### Performance Rules

Optimize Automatic Evolution Mode for uninterrupted useful progress:

- prefer narrow stages that each produce code, evidence, archive, and commit over large vague stages
- narrow stages must still close their declared target set; do not create a tiny version just to avoid unresolved tasks from the previous stage report
- after a paid/live experiment exposes a failure, make the next stage the smallest deterministic fix, then rerun or re-evaluate with bounded cost
- when a result file can be reused for deterministic re-evaluation, add an explicit result-path option instead of overwriting prior evidence
- keep the next task source in the stage report precise enough to start without asking the user
- treat a successful commit as a checkpoint, not as a stopping point
- keep the original experiment backlog visible until each experiment is completed, explicitly deferred, or superseded by a newer accepted experiment design
- when an experiment splits into implementation stages, add a return path to the original experiment question after the engineering fix lands
- never use an unfinished experiment as the basis for broad engineering improvement; finish the experiment, narrow the engineering change to an observed failure, or mark the decision as a hypothesis requiring validation
- report progress in commentary while working; reserve final answers for true pause, completion, or blockers

### 2. Split Themes into Working Plans

For complex work, create or update `docs/workingon/work_<topic>.md`.

The work file must contain:

- goal
- scope
- plan table
- linked current-design files
- acceptance criteria
- current status

Do not treat chat-only plans as sufficient for Lilies stage work. Do not put next-stage guidance in `workingon`; next-stage guidance belongs only in the stage report. `workingon` may record immediate execution status and evidence, but it must not decide the next version.

### 3. Expand Plans into Current Design

For each plan that needs implementation or review, create or update `docs/current-design/design_<component-or-flow>.md`.

The design must specify:

- goal
- module boundary
- data flow or control flow
- implementation plan
- acceptance criteria
- referenced intellectual assets

The design must not contain next-stage task selection, roadmap guidance, or instructions for what version to do next. A design is a concrete plan for one accepted task only.

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

Exception: in Automatic Evolution Mode, do not wait for separate user review. Archive and commit immediately after the stage is implemented and verified, then advance to the next version according to the mode rules.

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
- before archiving the current stage, audit the previous stage's historical design records; if the previous stage is incomplete, repair that archive first
- archive design files only after the corresponding stage/version state exists in a stage report or equivalent accepted version-state document
- if a user asks to archive design files before the corresponding version state exists, refuse the design archive first and ask to create or confirm the version state
- name historical designs with an explicit version or stage marker, for example `v0.2.3_design_platform_harness_task_monitor_v1.md`
- do not use dates as the primary historical design filename; dates may appear inside the file metadata, but the filename must be version/state based
- never overwrite an earlier historical design version; preserve design evolution across stages
- record source stage, original design path, final status, implementation evidence, verification, and remaining risk
- every small-version completion, for example `v0.2.9`, must recover the designs that define that version into `docs/historical-designs/` before the archive commit
- keep `docs/current-design/` as the active design workspace; clear or replace active designs only when the user explicitly requests cleanup or the next stage requires it
- after every small-version archive, active `docs/current-design/` must contain no `design_*.md` files; move completed, deferred, rejected, or superseded designs to `docs/historical-designs/`
- after every small-version archive, active `docs/workingon/` must contain no intermediate files; move work files, implementation evidence, question logs, raw experiment evidence, and other temporary results to a versioned archive such as `docs/workingon-archives/v0.2.18/`
- keep only active workspace README files in `docs/current-design/` and `docs/workingon/` after archive, unless the user explicitly asks to keep a new active task open

Do not discard `workingon` files during archive. Move them into a versioned archive, then clear the active workspace.

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

## Experiment Application Gate

Treat engineering changes derived from experiments as serious evidence-based work.

Before using an experiment result to change code, prompts, benchmark semantics, Harness policy, Builder behavior, Template behavior, or workflow-generation strategy:

1. Confirm the experiment is actually complete: question, setup, execution, result, conclusion, and evidence are recorded.
2. Confirm the experiment has the required report artifact. For Lilies completed experiments this normally means a concise `.docx` report plus raw evidence such as JSON, logs, screenshots, or stage report links.
3. Confirm the result is strong enough for the proposed engineering change. If the evidence is narrow, make a narrow fix for the observed failure rather than a broad architecture change.
4. If the experiment is incomplete, do not use it as a finished basis for engineering improvement. Either finish the experiment first, create a smaller bounded experiment, or explicitly label the engineering change as a hypothesis requiring validation.
5. After the engineering change, update the experiment report or the experiment-status ledger with an application supplement: applied marker, changed files/modules, stage report, tests, paid/live evidence if relevant, and remaining caveats.
6. Mark experiments already used for engineering as `已应用`; mark experiments used to validate an already-made improvement as `验证应用`.
7. Maintain the current version experiment status under `docs/experiment-status/` after every stage evolution.

The original experiment backlog is not closed by useful engineering progress alone. Close each backlog item only when its experiment question is answered, explicitly replaced by a newer experiment, or formally deferred/blocked with a next action.

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
