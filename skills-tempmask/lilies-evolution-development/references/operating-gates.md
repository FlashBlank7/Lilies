# Operating Gates

Use this reference for automatic evolution, full task expansion, design execution, closure checks, and continuation behavior.

## Campaign Priority Gate

- Completing and verifying the capability-boundary report is the highest objective.
- Stage reports sequence campaign work; they do not outrank or redefine the campaign.
- A local gate may limit a claim or keep one implementation task open. It may not stop unrelated authorized report intents unless no valid campaign route remains.
- Before stopping, enumerate remaining non-terminal report intents and prove that none has an actionable implementation, design, deterministic-test, or contract route.

## Evidence Ceiling Gate

Classify an unavailable browser, model, tool, customer tenant, or live service as an evidence condition, not automatically as implementation failure.

1. Probe the dependency once and record the exact result.
2. Record achieved evidence level, intended level, `blocked_by_environment`, claim ceiling, evidence debt owner, and a concrete recheck trigger.
3. Never report the unavailable level as passed.
4. Do not repeat the same probe until the recheck trigger or external state changes.
5. If the Stage Contract accidentally requires the unavailable provider itself, obtain or apply explicit user authority for a scoped contract revision. Preserve behavior acceptance and carry the higher evidence target as debt.
6. Continue from the next authorized report task after truthful scoped closure.

Only classify the campaign as blocked when the condition prevents every remaining authorized intent or requires user authority for safety, irreversible action, or product scope.

## Stage Authority

- `stage-reports/` is the only layer that may guide the next stage.
- `current-design/` is one accepted task expanded into a concrete implementation contract; it is not a roadmap.
- `workingon/` stores active execution evidence and intermediate results; it is not a roadmap.
- `workingon/` must not contain authoritative next-stage task decomposition. If it does, treat that content as non-authoritative notes and repair the next stage report.
- `intellectual-assets/` can justify designs but must not select the next version by itself.
- Do not select one convenient next-stage task and ignore the rest.

## Stage-report Task Authority Gate

Before implementation for every small version:

1. Read the Program Charter, intent registry, latest stage report's `Next-stage Task Set`, and `Automatic Evolution Handoff`.
2. Preserve every stable task ID and source intent ID when selecting the current version scope.
3. Lock `Stage Contract` before implementation: mandatory/optional class, surface/role, measurable acceptance criteria, and required evidence.
4. Only the user may approve a change to mandatory scope or acceptance. An implementation discovery may change route, but it must preserve acceptance and be recorded in `Deviations`.
5. Create one or more current-design files for every contracted task that needs engineering, product, experiment, docs-process, or review work.
6. Every current-design file must cite the source stage report and stable task ID.
7. If the task set is too large for one version, obtain user approval for a contract revision; do not silently drop tasks or move them through workingon.
8. Record disposition in `Source Task Set`, `Unresolved / Blocked / Deferred`, and `Intent Coverage`.
9. Do not advance versions while mandatory behavior is incomplete or unsupported at the contracted closure floor. Higher unavailable evidence may be carried only as explicit scoped debt under recorded user authority and a strict claim ceiling.

## Design Execution Gate

For each design:

1. Implement in code or relevant project files.
2. Record evidence in `docs/workingon/implementation_<topic>.md`.
3. Run focused verification.
4. If implementation exposes a design flaw, revise the design and continue the same design.
5. If implementation exposes a larger direction issue, revise the relevant current design and ensure the final stage report records the changed disposition. Do not turn workingon into a roadmap.
6. Proceed to the next design only after code, verification, evidence, and remaining risk are recorded.

## Version Advancement Gate

Before every archive, prove that the version is a serious closure unit. A valid version normally needs one of:

- multiple coordinated design contracts under a coherent stage goal,
- a vertical/product slice across backend/frontend/API/tests/docs/operator visibility,
- a research experiment with DOCX/report evidence, ledger/index update, and application marker,
- a platform boundary slice with enforcement, observability, persistence, controls, and tests,
- a P0 process/architecture repair that updates the skill, templates, gates, validation, and docs,
- or an explicit user/emergency/blocker exception recorded in `Stage Identity`.

One historical design in a version is allowed only with explicit justification in `Stage scope justification`. Repeated one-design versions are a process smell: consolidate scope or repair process architecture before continuing.

## Minor Version Completion Gate

Before every small-version archive:

1. Re-read the source stage report task set, relevant current designs, experiment ledgers, and the stage goal.
2. Ask whether the version genuinely completed the claimed target or only completed a prerequisite.
3. Check every design acceptance criterion and every experiment deliverable.
4. If the version objective includes an experiment, do not mark it complete without its required report artifact.
5. If an experiment result is used for engineering, confirm the ledger marks `已应用` or `验证应用`.
6. If only a prerequisite was completed, continue the current version with the next bounded implementation batch; do not increment the version.
7. If mandatory behavior is blocked, record the blocker and decision authority and keep the stage open. If only higher external evidence is unavailable, use a user-approved scoped contract revision, record evidence debt and claim ceiling, and continue the campaign.
8. Run a fresh-context closure review against the locked contract, diff, and evidence.
9. Validate the new stage report with `scripts/validate_stage_report_template.py` and `scripts/validate_evolution_control.py`.
10. Archive only when every mandatory task is completed, evidence is valid, intent coverage is explicit, and both the closure audit and version-size gate pass.

## Major Version Completion Gate

Before declaring a phase complete:

1. Verify a phase report exists under `docs/phase-reports/`.
2. Move every completed `v0.<minor>.*` stage report from active `docs/stage-reports/` into `docs/stage-report-archives/v0.<minor>.x/`.
3. Leave active `docs/stage-reports/` with only `README.md`, `STAGE_REPORT_TEMPLATE.md`, and current unarchived phase reports.
4. Add/update archive README files with range, count, latest handoff, next phase target, and unresolved blockers.
5. Update docs index and references so moved stage reports are not linked through the active directory.
6. If this archive fixes a process failure, update the skill and archive gates before committing.
7. Do not start the next major version until the stage-report set archive is complete.

A phase report without a stage-report set archive is an incomplete major-version archive.

## Engineering Capability Closure Gate

Classify closure level before implementation:

- `backend slice`: backend logic/API only.
- `vertical slice`: backend plus frontend/API usage plus tests.
- `platform boundary`: enforcement, observability, persistence, cancel/timeout/budget behavior, and failure reporting.
- `product capability`: user-facing workflow, UI, docs, tests, and operational evidence.
- `research experiment`: experiment report plus evidence chain.

Verify:

1. Source stage task, stage goal, current designs, and evidence agree on "done".
2. Promised code paths, error paths, and integrations are implemented.
3. Resource use, cancellation, budget, timeout, persistence, audit, and recovery are handled or explicitly out of scope.
4. User-facing capability has usable Studio/API surface unless explicitly backend-only.
5. Focused tests, regression tests, and live/paid acceptance are run when relevant.
6. Evidence, stage report, historical design, and experiment-status updates exist.
7. Missing mandatory pieces keep the stage open. Optional gaps may be marked `carried forward`, `deferred`, or `blocked` with a concrete next action and preserved source intent.

Platform Harness work must be named as a partial policy slice unless the full hard-boundary chain is closed.

## Design Archive Gate

Before starting a new version:

1. Identify the latest completed stage report and version.
2. Check that every referenced design has a historical record.
3. Repair missing historical design archives before beginning the next version unless the stage report explicitly rejected/deferred the design.

During each small-version archive:

- collect every current-design file created, revised, implemented, deferred, or rejected for the version,
- write historical designs as `docs/historical-designs/v<version>_design_<topic>_v<n>.md`,
- do not use dates as the primary filename,
- record source stage, original path, final status, evidence, verification, and remaining risk,
- update current-design and historical-design README inventories,
- include a `Historical Designs` section in the stage report,
- refuse archive or advance if no accepted version/state document exists.

Design recycling is part of archive completion.

## Continuation Gate

After every archive commit in Automatic Evolution Mode:

1. Re-read the Program Charter, intent registry, and just-committed stage report's `Next-stage Task Set` and `Automatic Evolution Handoff`.
2. Run the Minor Version Completion Gate against the completed version.
3. Run the Design Archive Gate against the completed version.
4. Run `git log --oneline -3` and `git status --short`.
5. If an active `Current task ID` exists, resume that task before interpreting any summary or next-step prose.
6. If the handoff says continue and a stable next task ID is authorized, do not final-answer. Select the declared next version, lock its contract, and start implementation.
7. If a lane-selection, phase-report, governance, cleanup, or other decision task has a stable ID in the task set, execute that task inside the declared serious version. Do not invent a tiny planning version.
8. Final answer only on explicit pause/stop, campaign terminal closure, or a proven campaign-wide blocker. A stage-local evidence ceiling is not enough.

Context pressure, long runtime, or fatigue is not a valid stop reason. Write a deterministic checkpoint, compact, and resume the same stable task and stage contract.

## Automatic Evolution Boundaries

- Do not use `git reset --hard`.
- Do not stage unrelated files, generated caches, `.tmp/`, lockfiles such as `uv.lock`, or user changes outside the stage.
- Do not invent cosmetic versions; each version needs a concrete task source.
- Do not advance a version merely because one useful design is done. Continue within the same version until the selected stage scope is a serious closure unit or record an explicit exception.
- Do not turn a mandatory task into optional, deferred, superseded, or complete without explicit user-approved contract revision.
- Do not make unbounded paid calls; set budgets and record evidence.
- Stop only for explicit user pause/stop, campaign terminal closure, irreversible action needing consent, a dependency that blocks every remaining report intent, unbounded cost, safety/privacy/legal risk, unrecoverable merge conflicts, or absent/contradictory campaign authority. A missing service for one evidence level is not enough. "No meaningful single next task" is not a stop condition when an authorized selection or meta-planning task exists.

## Performance Rules

- Prefer serious coherent stages that produce code, evidence, archive, and commit across the surfaces required by their contract.
- Use narrow implementation batches inside a stage, while keeping the full mandatory target set locked and visible.
- After a paid/live experiment exposes a failure, implement the smallest deterministic fix inside the current stage unless the authorized task set explicitly assigns a later stage, then rerun or re-evaluate.
- Reuse result files for deterministic re-evaluation instead of overwriting evidence.
- Keep the next task source precise enough to start without asking the user.
- Keep the original experiment backlog visible until each experiment is completed, deferred, superseded, or blocked.
- When an experiment splits into implementation stages, add a return path to the original experiment question.
