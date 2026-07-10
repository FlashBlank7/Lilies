# Operating Gates

Use this reference for automatic evolution, full task expansion, design execution, closure checks, and continuation behavior.

## Stage Authority

- `stage-reports/` is the only layer that may guide the next stage.
- `current-design/` is one accepted task expanded into a concrete implementation contract; it is not a roadmap.
- `workingon/` stores active execution evidence and intermediate results; it is not a roadmap.
- `intellectual-assets/` can justify designs but must not select the next version by itself.
- Do not select one convenient next-stage task and ignore the rest.

## Full Task Set Expansion Gate

Before implementation for every small version:

1. Read the latest stage report's `Next-stage Tasks` and `Automatic Evolution Handoff`.
2. List every next-stage task in the active workingon file.
3. Disposition every task as `accepted`, `blocked`, `deferred`, or `superseded`.
4. Create one or more current-design files for every accepted task that needs engineering, product, experiment, docs-process, or review work.
5. If the task set is too large for one version, split it explicitly with reasons and preserve the full task set.
6. Do not advance versions until every accepted design is completed, revised and completed, or explicitly blocked/deferred with evidence.

## Design Execution Gate

For each design:

1. Implement in code or relevant project files.
2. Record evidence in `docs/workingon/implementation_<topic>.md`.
3. Run focused verification.
4. If implementation exposes a design flaw, revise the design and continue the same design.
5. If implementation exposes a larger direction issue, update the active workingon plan before continuing.
6. Proceed to the next design only after code, verification, evidence, and remaining risk are recorded.

## Minor Version Completion Gate

Before every small-version archive:

1. Re-read active workingon, relevant current designs, experiment ledgers, and the stage goal.
2. Ask whether the version genuinely completed the claimed target or only completed a prerequisite.
3. Check every design acceptance criterion and every experiment deliverable.
4. If the version objective includes an experiment, do not mark it complete without its required report artifact.
5. If an experiment result is used for engineering, confirm the ledger marks `已应用` or `验证应用`.
6. If only a prerequisite was completed, continue the current version with the smallest next slice.
7. If blocked, record `blocked`, `deferred`, or `carried forward` with the next concrete action.
8. Archive only when all accepted targets are completed or explicitly dispositioned with evidence.

## Engineering Capability Closure Gate

Classify closure level before implementation:

- `backend slice`: backend logic/API only.
- `vertical slice`: backend plus frontend/API usage plus tests.
- `platform boundary`: enforcement, observability, persistence, cancel/timeout/budget behavior, and failure reporting.
- `product capability`: user-facing workflow, UI, docs, tests, and operational evidence.
- `research experiment`: experiment report plus evidence chain.

Verify:

1. Stage goal, workingon, and design agree on "done".
2. Promised code paths, error paths, and integrations are implemented.
3. Resource use, cancellation, budget, timeout, persistence, audit, and recovery are handled or explicitly out of scope.
4. User-facing capability has usable Studio/API surface unless explicitly backend-only.
5. Focused tests, regression tests, and live/paid acceptance are run when relevant.
6. Workingon evidence, stage report, historical design, and experiment-status updates exist.
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
