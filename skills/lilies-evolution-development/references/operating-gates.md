# Operating Gates

Use this reference for automatic evolution, full task expansion, design execution, closure checks, and continuation behavior.

## Stage Authority

- `stage-reports/` is the only layer that may guide the next stage.
- `current-design/` is one accepted task expanded into a concrete implementation contract; it is not a roadmap.
- `workingon/` stores active execution evidence and intermediate results; it is not a roadmap.
- `workingon/` must not contain authoritative next-stage task decomposition. If it does, treat that content as non-authoritative notes and repair the next stage report.
- `intellectual-assets/` can justify designs but must not select the next version by itself.
- Do not select one convenient next-stage task and ignore the rest.

## Stage-report Task Authority Gate

Before implementation for every small version:

1. Read the latest stage report's `Next-stage Task Set` and `Automatic Evolution Handoff`.
2. Select the current version scope from that stage-report task set.
3. Create one or more current-design files for every accepted task that needs engineering, product, experiment, docs-process, or review work.
4. Every current-design file must cite the source stage report and source task.
5. If the task set is too large for one version, split the version scope explicitly and preserve all non-accepted tasks for final stage-report disposition.
6. Record accepted/blocked/deferred/superseded disposition in the completed stage report's `Source Task Set` and `Unresolved / Blocked / Deferred` sections.
7. Do not advance versions until every accepted design is completed, revised and completed, or explicitly blocked/deferred with evidence.

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
6. If only a prerequisite was completed, continue the current version with the smallest next slice.
7. If blocked, record `blocked`, `deferred`, or `carried forward` with the next concrete action.
8. Validate the new stage report against `docs/stage-reports/STAGE_REPORT_TEMPLATE.md` when possible.
9. Archive only when all accepted targets are completed or explicitly dispositioned with evidence.

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
7. Missing pieces are marked `carried forward`, `deferred`, or `blocked` with concrete next action.

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

1. Read the just-committed stage report's `Next-stage Tasks` and `Automatic Evolution Handoff`.
2. Run the Minor Version Completion Gate against the completed version.
3. Run the Design Archive Gate against the completed version.
4. Run `git log --oneline -3` and `git status --short`.
5. If the handoff says continue, or any next-stage task is concrete and unblocked, do not final-answer. Select the next version and start implementation.
6. If no single implementation task is preselected but the stage report contains lane-selection, phase-report, governance, cleanup, or other meta tasks, open the smallest next stage that resolves the decision instead of stopping.
7. Final answer only on explicit pause/stop or a real blocker.

Context pressure, long runtime, or fatigue is not a valid stop reason. Shrink the next stage and continue safely.

## Automatic Evolution Boundaries

- Do not use `git reset --hard`.
- Do not stage unrelated files, generated caches, `.tmp/`, lockfiles such as `uv.lock`, or user changes outside the stage.
- Do not invent cosmetic versions; each version needs a concrete task source.
- Do not advance a version merely because one useful design is done. Continue within the same version until the selected stage scope is a serious closure unit or record an explicit exception.
- Do not make unbounded paid calls; set budgets and record evidence.
- Stop only for explicit user pause/stop, irreversible action needing consent, missing credentials/services, unbounded cost, safety/privacy/legal risk, merge conflicts, or no valid next-stage source. "No meaningful single next task" is not a stop condition when a selection or meta-planning task exists.

## Performance Rules

- Prefer narrow stages that produce code, evidence, archive, and commit.
- Narrow stages must still close their declared target set.
- After a paid/live experiment exposes a failure, make the next stage the smallest deterministic fix, then rerun or re-evaluate.
- Reuse result files for deterministic re-evaluation instead of overwriting evidence.
- Keep the next task source precise enough to start without asking the user.
- Keep the original experiment backlog visible until each experiment is completed, deferred, superseded, or blocked.
- When an experiment splits into implementation stages, add a return path to the original experiment question.
