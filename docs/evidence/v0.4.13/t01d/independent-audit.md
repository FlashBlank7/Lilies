# V04-13-T01D independent Closure Audit

- Auditor: fresh read-only subagent context
- Audited source state: final T01D working tree before archive commit
- Verdict: **PASS**
- Audited at: 2026-07-22T23:09:30Z

The auditor reconstructed T01D from the locked Stage Contract and the expanded
pipeline design. Earlier review rounds found two mandatory gaps: semantic
compaction could not preserve more than one hundred unresolved reports, and a
production `create_app` instance had no trusted commit/evidence resolvers for
an implemented `DeveloperResponse`. Both gaps were fixed and re-audited; no
mandatory T01D implementation or evidence gap remains.

## Final acceptance reconstruction

- Collaboration is created only for an explicitly enabled, user-notified
  formal assignment. Ordinary assignments, tool catalogs, prompts, OpenAPI and
  unknown-route errors do not reveal the surface.
- Dedicated SQLite tables and transactions own channel sequence, messages,
  reports, decisions, immutable developer inbox entries, reader cursors,
  leases, responses, claims, verification, audit and operation receipts.
- Report routing and every state transition are enum- and role-driven. Manual
  approval, task-local auto-forward, permission separation and preapproval
  developer invisibility fail closed.
- Exact request digests, compare-and-set revisions and durable cursors make
  retries, reconnect, overflow and lease failure recoverable without duplicate
  messages or side effects.
- An implemented developer response requires a full commit reachable from the
  platform repository's current history and every evidence blob to belong to
  that exact commit tree with an exact SHA-256 digest. Missing commit/blob or a
  digest mismatch leaves the report implementing and its lease active.
- Lilies must refetch a changed contract before re-probing. Task-package and
  environment responses use their dedicated routes; permission denial cannot
  become a platform capability gap.
- Claims freeze the exact application draft and are atomically invalidated by
  a later draft write. Only the independent verifier may complete an existing
  claim after channel close.
- Daemon restart and compaction preserve collaboration wait state, remote
  side-effect intent/result, original business goal, decisions, contract
  changes, attempted routes/evidence, draft/run IDs, claim verdicts and the
  prohibition on substitute validation.
- Causal export is reconstructed from the collaboration tables without log
  scanning. A 121-report counterexample remains below the 30,000-character
  model boundary without losing report identity, goal, route, outcome or
  evidence identity.

## Verification reviewed

- Focused collaboration suite: 123 passed, 2 warnings, 0 failures.
- Former-blocker/state-persistence counterexamples: 6 passed, 1 warning.
- Full repository regression: 1219 passed, 85 expected xfails, 2 warnings, 0
  failures, using an ephemeral `python-docx` dependency because the existing
  dev extra does not declare it.
- Targeted Ruff, `git diff --check` and evolution-control validation: pass.
- One hundred overflow/reconnect rounds, one hundred lease-fault rounds and
  one hundred concurrent identical submitters retain zero loss and zero
  duplicate side effects.

## Claim ceiling

T01D proves the collaboration protocol, persistence, access control, routing,
approval, recovery, developer response existence gate, claim lifecycle and
causal export at deterministic in-process, real-ASGI HTTP, SQLite and Git
object levels. It does not claim the T01E monitor UI, T01F task-package/oracle
process, T01G qualification bundle or T01H enterprise experiment.
