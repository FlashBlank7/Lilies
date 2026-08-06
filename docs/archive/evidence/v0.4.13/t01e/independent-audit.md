# V04-13-T01E Independent Read-Only Audit

## Audit 1 — evidence-integrity failure

- Verdict: `FAIL — EVIDENCE INTEGRITY ONLY`
- Audit context: fresh read-only context, reconstructed from the Program Charter,
  locked Stage Contract, relevant diff, and verification evidence
- Implementation commit: `3ba23eac93a0ea909b8b7d0c0b292bdf07f9b1cb`
- Browser provider: `[]`; retained as `V0413-ED-003`, not retried and not treated
  as a campaign blocker

The reviewer found no remaining code or contracted-behavior blocker at the
scoped deterministic/API/CLI/frontend-build evidence floor. It rejected the
closure because `manifest.json` and the stage report still contained the
pre-repair `34/430/24` snapshot while refreshed evidence reported
`37/433/25`, and all changed evidence hashes were stale.

### Contract reconstruction and blocker recheck

- The global Developer Studio exposes task, observable Lilies context,
  tool/contract/draft/run/trace state, complete report causality, owner, reason,
  and next action.
- Capability approval and one-shot runtime permission use distinct controls,
  APIs, and idempotency signatures.
- Preapproval developer projection reveals no report identity, body, evidence,
  digest, or task-local count; only the allowed global `pending_user_action`
  boolean remains.
- `lilies-developer` implements `inbox`, `lease`, `renew`, `release`, and
  `respond` with strict JSON and a 0600/no-follow credential boundary.
- Customer Runtime uses a bounded projection that excludes
  developer/collaboration/private/oracle/credential fields and private failure
  text while preserving business output.
- Exact permission request IDs, null Assignment context, reader-cursor CAS,
  credential/private error handling, complete evidence and contract rendering,
  the 980px responsive boundary, modal focus trapping, and the five-second
  visible-page context refresh all have implementation and regression coverage.

### Audit 1 claim ceiling

Supported: deterministic service/API behavior, developer CLI contract, final
source HTTP Studio/inbox/Customer Runtime projections, frontend source,
TypeScript, and production build in a controlled local single-user
environment.

Not supported: real desktop/mobile browser layout or interaction, keyboard,
overflow, reduced-motion, console/network, screenshots, Customer Runtime DOM
isolation, real live lease/respond mutation, production IAM, multi-tenancy, or
production deployment.

## Remediation before Audit 2

- Evidence counts refreshed to `37 focused`, `433 broad`, `25 frontend`, and
  `1302 passed / 85 xfailed / 0 failed` full repository.
- Final-source HTTP and CLI evidence binds commit `3ba23ea`.
- Stage-report verification rows and claim ceiling were refreshed without
  claiming the unavailable browser layer.
- Manifest hashes and result counters are regenerated after this record.

## Audit 2 — final fresh-context verdict

- Verdict: `PASS AT THE SCOPED EVIDENCE FLOOR`
- Audit context: a second fresh read-only context independently reconstructed
  T01E from the Program Charter, locked Stage Contract, baseline-to-final
  implementation diff, and refreshed evidence
- Implementation commit: `3ba23eac93a0ea909b8b7d0c0b292bdf07f9b1cb`
- Browser provider: unchanged `[]`; retained as `V0413-ED-003` without retry

Audit 2 verified that all six manifest SHA-256 values matched their current
file bytes and that the counters were internally consistent at `37 focused`,
`433 broad`, `25 frontend`, and `1302 passed / 85 xfailed / 0 failed` for the
full repository. It confirmed that baseline `02e003e` is an ancestor of
implementation commit `3ba23ea` and that the final-source HTTP and CLI
evidence consistently binds that implementation.

The reviewer reconstructed and accepted the locked behavior for semantic
Studio context and causality, desktop three-pane and mobile three-level source
contracts, separate runtime-permission and capability-approval controls,
preapproval developer non-disclosure, the
`inbox/lease/renew/release/respond` CLI contract, and the bounded Customer
Runtime projection. It also rechecked the eight repaired blocker families:
exact permission-request matching, null Assignment handling, reader-cursor CAS
races, credential and Authorization-form redaction, bounded private failure
errors, complete evidence/contract/DeveloperResponse/verifier rendering, the
980px responsive boundary with modal focus containment, and the visible-page
five-second context refresh.

No mandatory T01E blocker remains at this evidence floor.

### Final claim ceiling

Supported: deterministic service/API behavior, the strict developer CLI
contract and deterministic mutation coverage, final-source local HTTP
Studio/inbox/Customer Runtime projections, frontend source contracts,
TypeScript, lint, and production build in a controlled local single-user
environment.

Not supported: real desktop/mobile browser rendering or interaction, keyboard,
overflow, reduced-motion, console/network, screenshots, Customer Runtime DOM
isolation, a live successful lease/respond mutation, production IAM,
multi-tenancy, or production deployment.
