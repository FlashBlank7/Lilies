# Lilies Repository Instructions

## Campaign Supremacy

- The highest-priority objective is to implement and verify every intent in `docs/lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx` as tracked by `docs/evolution-control/report_intents.json`.
- A stage report, Stage Contract, test gate, archive rule, evidence provider, or agent workflow exists to advance that objective. It must not become a competing objective or freeze unrelated report work.
- Authority order is: latest explicit user instruction, report campaign objective and intent registry, stage-report sequencing, Stage Contract, current design, working evidence.
- The latest valid stage report remains the only next-task sequencing source, but it is subordinate to the campaign objective and cannot redefine, narrow, or indefinitely stall it.
- When an external evidence surface is unavailable, record the achieved evidence level, `blocked_by_environment`, claim ceiling, and recheck trigger. Continue implementation from an authorized report intent. Do not call the campaign blocked merely because one evidence provider is unavailable.
- A campaign blocker exists only when no remaining report intent has a valid implementation, design, deterministic-test, or contract route, or when user authority is required for safety, irreversible action, or a genuine product-boundary decision.
- Do not retry an unchanged external blocker across turns. Probe once, persist evidence, and resume report implementation until an external-state change makes a retry meaningful.

## Report Application Campaign

Before substantial Lilies work, read in this order:

1. `docs/evolution-control/PROGRAM_CHARTER.md`
2. the latest valid `docs/stage-reports/v*.md`
3. that report's locked `Stage Contract`
4. only the current task's relevant `docs/current-design/` and `docs/workingon/` evidence

## Authority

- The report campaign objective is the highest product authority. The latest valid stage report is the only source of next-stage task sequencing and version selection beneath that objective.
- `docs/workingon/` contains intermediate results and evidence only. It must not define a next-stage task set.
- `docs/current-design/` expands an accepted stage task. It does not select another task or version.
- `docs/evolution-control/PROGRAM_CHARTER.md` constrains intent and completion but is not a task source.
- Every current and next task uses a stable task ID and source intent IDs from `docs/evolution-control/report_intents.json` or a newer explicit user instruction.

## Completion

- Continue the current mandatory task until its acceptance and required evidence are complete.
- Do not turn a mandatory task into deferred, optional, superseded, or out of scope without user-approved contract revision.
- `blocked`, `not_run`, `partial`, `documented`, and `deferred` are not completion.
- Do not archive or advance a version unless Closure Audit is `pass`, the version-size gate is `pass`, and `scripts/validate_evolution_control.py` passes.
- For a version Closure Audit, spawn one read-only reviewer in a fresh context. Give it only the Program Charter, current stage report, relevant diff, and verification evidence; it must reconstruct requirements from the Stage Contract and report missing work before the implementing agent updates the verdict.
- Do not claim the report-application campaign complete unless `scripts/validate_evolution_control.py --campaign-closure` passes.
- Product behavior depending on browser, real model, real tool, or external integration needs evidence at that claimed level.
- Missing higher-level evidence limits the claim; it does not erase deterministic implementation evidence or automatically block the campaign. Track it as evidence debt and never report the unavailable level as passed.

## Deviation And Resume

- Implementation-route changes are allowed when acceptance is unchanged and the deviation is recorded.
- Goal, product boundary, target user, priority, or mandatory acceptance changes require user approval.
- On startup, resume, or compaction, reload the charter, current Stage Contract, current task ID, checkpoint, and git status before planning.
- If a mandatory task is open, resume it. Do not invent a new next step from a summary.
- If the task is open only because an external evidence level is unavailable, use the latest user-approved priority rule to revise the contract to a truthful scoped closure, preserve the evidence debt, and continue the report campaign.

## Working Tree

- Preserve unrelated user changes in the dirty worktree.
- Never use destructive git commands to simplify the stage.
- The user deleted the Lilies evolution Skill and explicitly forbids restoring, editing, or using it for this campaign. Repository instructions, intent registry, code, tests, and deterministic validators are the complete execution mechanism.
