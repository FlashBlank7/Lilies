# Lilies Repository Instructions

## Report Application Campaign

Before substantial Lilies work, read in this order:

1. `docs/evolution-control/PROGRAM_CHARTER.md`
2. the latest valid `docs/stage-reports/v*.md`
3. that report's locked `Stage Contract`
4. only the current task's relevant `docs/current-design/` and `docs/workingon/` evidence

## Authority

- The latest valid stage report is the only source of next-stage tasks and version selection.
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

## Deviation And Resume

- Implementation-route changes are allowed when acceptance is unchanged and the deviation is recorded.
- Goal, product boundary, target user, priority, or mandatory acceptance changes require user approval.
- On startup, resume, or compaction, reload the charter, current Stage Contract, current task ID, checkpoint, and git status before planning.
- If a mandatory task is open, resume it. Do not invent a new next step from a summary.

## Working Tree

- Preserve unrelated user changes in the dirty worktree.
- Never use destructive git commands to simplify the stage.
- The user intentionally temp-masked the previous Lilies Skill. Do not restore or activate it unless explicitly requested; these repository instructions and deterministic validators remain active independently.
