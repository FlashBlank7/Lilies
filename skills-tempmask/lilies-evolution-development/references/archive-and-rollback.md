# Archive And Rollback

Use this reference for workingon archive, historical-design recycling, archive commits, and documentation-only rollback.

## Archive Protocol

Normal mode: wait for the user to request archive. Automatic Evolution Mode: archive and commit after verification, then continue.

Stage archive output:

- compact stage report under `docs/stage-reports/`,
- historical design records under `docs/historical-designs/`,
- versioned workingon archive under `docs/workingon-archives/v<version>/`,
- updated `docs/current-design/README.md`, `docs/workingon/README.md`, and `docs/historical-designs/README.md` when inventories change,
- updated experiment status index/ledgers when experiments were created, run, applied, verified, blocked, deferred, or superseded.

Major-version archive output:

- phase report under `docs/phase-reports/`,
- completed stage-report set moved from active `docs/stage-reports/` to `docs/stage-report-archives/v0.<minor>.x/`,
- archive README at `docs/stage-report-archives/README.md`,
- per-phase archive README at `docs/stage-report-archives/v0.<minor>.x/README.md`,
- active `docs/stage-reports/README.md` updated to show the current phase state,
- docs index and moved-link references updated,
- unresolved blockers and next-phase entry recorded in both the phase report and latest handoff stage report.

## Current Design Recycling

During every small-version archive:

- archive every relevant `docs/current-design/design_*.md` that belonged to the version,
- include implemented, revised, deferred, rejected, and superseded designs,
- write historical files as `docs/historical-designs/v<version>_design_<topic>_v<n>.md`,
- do not use dates as primary filenames,
- never overwrite earlier historical design versions,
- after archive, active `docs/current-design/` must contain no `design_*.md` files unless a new active task is explicitly open.

Historical design records should be final design contracts only: problem, boundary, final solution, acceptance, final status, and evidence links. Do not repeat experiment results or stage summaries.

## Workingon Recycling

Move active execution files into a versioned archive, for example `docs/workingon-archives/v0.2.45/`.

Archive:

- `work_*.md`,
- `implementation_*.md`,
- `experiment_*.md`,
- question logs,
- intermediate reports,
- command evidence that matters.

Do not discard workingon files. After archive, active `docs/workingon/` should contain only README files unless a new active task is explicitly open.

## Compact Stage Report

Stage reports are compact factsheets. They guide the next stage, but they should not contain command transcripts or raw experiment details. Put details in workingon archive, ledgers, and summary evidence.

Required shape:

1. Goal
2. Completed
3. Evidence links
4. Verification
5. Unfinished / carried forward
6. Next-stage tasks
7. Historical designs
8. Workingon archive
9. Archive commit
10. Automatic Evolution Handoff when relevant

## Archive Commit Rules

After a valid archive, automatically commit unless the user explicitly says not to.

1. Run `git status --short`.
2. Build an explicit path list.
3. Never use `git add .`, `git add -A`, or broad directory staging when unrelated changes exist.
4. Do not stage unrelated source code, unrelated docs, local runtime output, `.tmp/`, generated caches, lockfiles such as `uv.lock`, or user changes.
5. Verify staged set with `git diff --cached --name-status`.
6. Use a version/state based commit message, for example `docs: archive v0.2.45`.
7. Report the commit hash.

If git is unavailable, hooks fail, or the repo is conflicted, report the blocker and leave files uncommitted.

## Docs-only Rollback Protocol

This protocol only rolls back documentation. It must not roll back source code, database migrations, dependency lockfiles, generated runtime artifacts, local caches, or live application state.

1. Confirm documentation-only scope.
2. Identify target docs baseline before mutating files. Prefer explicit commit hash; otherwise infer from stage report and `git log -- docs skills`.
3. Use file-level restoration and deletion. Do not use `git reset --hard`, do not rewrite history, and do not delete unrelated user changes.
4. Classify affected files into delete, restore, and keep lists.
5. Keep unrelated untracked files such as `uv.lock`.
6. Keep `skills/lilies-evolution-development/` unless the user explicitly asks to remove the project skill.
7. Run structural checks, compare against the target baseline, validate the skill if changed, and report expected diffs.

Use the rollback template in `templates.md`.
