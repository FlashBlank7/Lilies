# V04-13-T01D Independent Read-Only Closure Audit

- Captured at (UTC): `2026-07-23T00:59:33Z`
- Baseline commit: `91d68006de539df050b84c3791894b8dc81aaa63`
- Reviewer context: fresh read-only sub-agent given the Program Charter, current
  stage report and locked Stage Contract, the T01D-relevant diff, and final
  verification evidence
- Verdict: `PASS for V04-13-T01D only`

The reviewer reconstructed T01D from the locked contract. No mandatory T01D
implementation or verification work remains. This verdict does not close
v0.4.13 because T01E-J remain mandatory.

The audit independently confirmed:

- formal-task-only activation and ordinary-session nondiscoverability;
- dedicated persistence, transactional sequencing, CAS, full-request
  idempotency, durable cursors, access control, exclusive recoverable leases,
  causal export, and verifier-only completion of an existing claim after close;
- deterministic routing, approval and task-local auto-forward separation,
  complete preapproval developer isolation, task/environment responses,
  Lilies re-probe, claim freeze/invalidation, and daemon restart/deadline and
  uncertain-side-effect recovery;
- production `create_app` enforcement of a full commit reachable from current
  Git history plus same-tree `gitblob` content and SHA-256 evidence, including
  fail-closed negative paths that retain the lease and persist no response;
- bounded JSON history replay that cannot mutate the durable acknowledgement
  and applies exact-sender claim visibility before SQL pagination;
- compaction provenance that rejects forged transcript state and trusts only
  transaction-linked collaboration updates or paired successful public-tool
  results;
- exact schema-max claim and workflow recovery without adding a fourth
  collaboration tool;
- a paginated workflow archive index plus exact `state_digest_b64` selection;
  missing or ambiguous state selectors fail closed instead of selecting the
  transcript tail;
- current-claim order based on claim creation sequence, so a later result or
  invalidation for an older claim cannot displace the latest claim;
- strict Git tree parsing that rejects a blob identity embedded in filename
  bytes; and
- a durable retry test driven by an injected logical clock while retaining
  stale-owner and revision-fencing assertions.

The adversarial counterexamples that initially failed are closed:

- App A with 500 run IDs followed by an App B inspection now recovers App A's
  IDs exactly through App A's indexed state selector.
- `claim1 -> claim2 -> claim1 invalidation` retains claim2 as
  `claim_current_ref`.
- Approval-shaped local or unprovenanced records cannot be omitted as remotely
  replayable.
- User-forged workflow JSON, unmatched tool results, conflicting contract
  digests, and late old-run results do not become authoritative workflow state.

Evidence reviewed at the final stable implementation state:

- T01D focused: `147 passed, 2 warnings in 25.87s`;
- independent compaction re-audit: `32/32 passed`;
- independent archive/tool slice: `17/17 passed`;
- storage/security: `21 passed in 2.05s`;
- durable logical-clock suite: `9 passed, 1 warning in 5.14s`;
- full repository: `1243 passed, 85 expected xfailed, 2 warnings in 242.33s`;
- targeted Ruff, `git diff --check`, and evolution-control validation: pass;
- aggregate SHA-256 over the 17 T01D source/test file hashes:
  `a0dc30ee4b26b8b849c6f220d2e7405c3bc90f9e8da843f92b273e25c1b550cd`,
  with individual hashes stable across the full regression.

The claim ceiling is deterministic in-process, real ASGI/HTTP, SQLite, and
Git-object proof of the T01D collaboration protocol. Compaction claims are
limited to the tested bounds, including 121 reports/claims/workflows, 200
decisions, schema-max 500 test plus 500 business run IDs, and multi-application
exact recall. This audit does not claim the T01E monitoring UI/developer CLI,
T01F immutable task-package/oracle/verifier process, T01G machine qualification
bundle, T01H enterprise experiment, or production/multi-tenant reliability.

The default dev extra still omits `python-docx`; the full regression passed with
ephemeral `--with python-docx --frozen` without modifying the user's `uv.lock`.
