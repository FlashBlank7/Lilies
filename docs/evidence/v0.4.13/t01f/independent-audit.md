# V04-13-T01F Independent Read-Only Closure Audit

- Verdict: `PASS`
- Audit context: fresh read-only root context with two independent read-only
  subreviews
- Authorized inputs: Program Charter, locked Stage Contract, implementation
  diff, and verification results
- Baseline commit: `327f7458732996aabfa8356da5ffa12d01dc6a4a`
- Implementation commit: `d51e062bfbf0e0f96fb5e5f29d39a1a1a9d7b986`
- Audit evidence floor: deterministic and controlled-local macOS integration
- Files modified by reviewers: none

## Contract reconstruction

The reviewer independently reconstructed T01F as requiring:

1. strict immutable task revisions with schema, content, permission, parent,
   environment, policy, and verification-process digests;
2. authenticated environment preflight that cannot issue readiness from
   missing, failed, stale, or substitute health evidence;
3. frozen allowed actions and durable budgets that callers, providers,
   workspaces, retries, concurrency, or compensation cannot widen;
4. role-filtered Lilies, developer, and verifier workspaces excluding Git
   metadata, platform data, protected/oracle material, secrets, aliases, links,
   and mount escapes;
5. platform-owned append-only archives for success and every terminal failure,
   with exact byte, mode, index, typed-semantic, and claim replay;
6. frozen v1.1 claim hashes and rejection of old-revision mutation, legacy or
   payload-only claims, protocol mocks, forged server fields, unregistered
   verifier processes, and substitute validation;
7. a fresh read-only independent verifier whose source, interpreter, dependency
   inventories, sandbox, read roots, and single output sink are frozen;
8. oracle-leak prevention before freeze, workspace exposure, successful
   archive, and result publication; and
9. replayable stable hidden-seed aggregation that callers cannot forge and old
   successful attempts cannot resurrect after failure.

## Findings

The audit found implementation and negative-test coverage for every item:

- `task_packages.py` freezes and reloads strict content-addressed revisions,
  authenticated readiness, role projections, archives, replay, and full claim
  bindings.
- Formal assignment, workspace, worker, source-provenance, promotion,
  collaboration-budget, connector-budget, and run-archiver paths persist
  authority and side-effect boundaries across retry, restart, concurrency, and
  terminal failure.
- The verifier runs through a frozen `python -S` source/runtime bundle under a
  deny-by-default macOS sandbox, cannot read another oracle or mutate inputs,
  and fails before execution on same-version dependency-byte drift.
- Scanner coverage includes raw, JSON, embedded, Unicode, base64, URL-safe
  base64, hex, protected markers, and task-specific adapter/mapping/final-graph
  semantics.
- Stable verification derives seed identity through a platform resolver,
  requires distinct seed/assignment/archive/claim records, repeats trusted
  verification, and deterministically exports a secret-free read-only bundle.

No mandatory T01F blocker remains.

## Verification reviewed

The audit received:

- changed test run: `561 passed, 2 warnings`;
- final v0.4.13 run: `830 passed, 2 warnings`;
- final repository run: `1668 passed, 85 xfailed, 2 warnings`;
- Ruff on 76 changed Python paths: passed;
- compileall and `git diff --check`: passed.

After the implementation commit was frozen, the implementing root also ran the
22 newly added T01F test files: `342 passed, 1 warning, 0 failed`.

## Claim ceiling and retained evidence debt

The PASS applies only to deterministic and controlled-local macOS integration:

- `real_host` proves authenticated non-mock local health, not an external
  customer, Paperless, InvenTree, or production host;
- process isolation currently depends on macOS `sandbox-exec` and fails closed
  elsewhere, so cross-platform or production-grade containment is not claimed;
- the Paperless-ngx 2.20.15 / InvenTree 1.4.2 real environment, continuous
  enterprise model session, OCR/matching/Human Input/writeback/receipt/XLSX,
  model provider and cost, 36/36 oracle, and three actual independent seeds
  remain mandatory T01H evidence;
- the full T01H archive and proof that Codex authored no task-specific adapter,
  mapping, or final graph remain outstanding.

These are higher-level T01H evidence debts, not missing T01F mechanisms. The
reviewer therefore returned `PASS` without expanding the product or environment
claim.
