# V04-13-T01K independent Closure Audit

## Audit 1 — historical `PARTIAL`

An earlier read-only evidence review did not authorize closure. It required
the token result to be backed by a daemon-global receipt and complete startup
safety evidence rather than only client-attributable detail, and it required
the final platform implementation and follow-up repairs to be commit-bound.
Those gaps remained mandatory until the authenticated global observability
bridge, global-authoritative monitor, legacy unknown-call migration, final
process detection, final commits, and refreshed regressions were complete.

## Audit 2 — `PASS`

Date: 2026-07-26

The final reviewer ran in a fresh, read-only context. It was restricted to
the Program Charter, current stage report, locked r6 Stage Contract, the
current T01K evidence directory, and the explicitly listed T01K commits in
the platform and sibling repositories. It did not use current-design,
workingon, or an implementation summary as task authority.

Verdict: `PASS at the scoped evidence floor`

Mandatory blockers: `0`

This verdict closes only `V04-13-T01K`. It does not close v0.4.13, T01H, or
T01L-N and it remains `enterprise_denominator=false`.

### Contract reconstruction and finding

| r6 requirement | Finding |
| --- | --- |
| Standalone sibling repository, distribution, Git history, daemon, CLI, private state, tests, and SwiftPM application | Passed. `../LiliesAgent` has an independent continuous history through `2272704aad2b319099fa21bbae51685b922e1aa2`; the isolated wheel started a real daemon from `site-packages`. |
| No `agent_platform` runtime import or shared platform database/API token | Passed. The standalone runtime scan and committed boundary use authenticated loopback HTTP only. |
| Safe discovery | Passed. Directory-descriptor anchoring, `O_NOFOLLOW`, owner/mode/type/size/inode/parent checks, literal loopback origin, listener PID, distribution ID, and public fingerprint are enforced with negative coverage. |
| Explicit one-time pairing and secret boundary | Passed. Discovery grants no authority; the real bridge used exact scopes and a one-time code. The platform neither reads nor records the daemon bootstrap secret. |
| Real separate-process bridge and replay | Passed. The sibling daemon process, pairing, assignment, SSE relay/ack, cancellation, bridge reconstruction, and exact event replay are covered without provider egress. |
| Provider disabled by default; startup and idle consume no model Token | Passed. A hard model-access gate is present. The authenticated 5.709-second dual capture had zero token/call/usage-record/unknown/cost delta and zero startup automatic consumers. |
| Persisted session/stage/model usage and honest unknown handling | Passed. New attempts reserve a durable call ID and dimensions. Missing usage remains unknown. The seven legacy turns produce exactly 123 capped, resumable, savepoint-protected and idempotent unknown entries without invented attribution. |
| Desktop create/attach/resume/stop, messages, permissions, Token display, and daemon restart recovery | Passed at the functional evidence level. Swift reported 93 passing tests, including a real Python daemon restart integration preserving pairing, session, ack cursor, messages, selected state, and usage. |
| Package, Python, CLI, restart, Swift, build, launch, manifests, and regression | Passed. Standalone Python reported `231 passed, 9 skipped`; the final platform T01K set reported `352 passed`; package and app artifacts were reproducible and their recorded hashes matched. |
| Claim and denominator boundary | Passed. The evidence explicitly excludes T01L-N, T01H/customer success, public macOS distribution, account-wide Codex billing, and every unavailable visual claim. |

### `V0413-DEV-020` and `V0413-ED-004`

The native Computer Use pipe failed twice with the same external startup
error. Repeating the unchanged probe would violate `EVOL-008`. `EVOL-007`
permits a scoped evidence ceiling when an external provider is unavailable,
provided that implemented behavior remains verified and the unavailable
level is retained as debt.

The reviewer therefore accepted `V0413-DEV-020` as an evidence-route-only
deviation and retained `V0413-ED-004` as `blocked_by_environment`. Native
build, local ad-hoc signing, strict signature verification, exact packaged
process launch, Swift behavior tests, and real daemon restart integration are
valid evidence for their own levels. They are not visual inspection.

The audit makes no claim that layout, visual quality, human interaction,
keyboard behavior, accessibility, or native UX passed. Any later report that
describes those as passed without new evidence would exceed this verdict.

### Non-blocking evidence debt and limits

- `V0413-ED-004`: exact packaged native visual, interaction, keyboard, and
  accessibility inspection; recheck when the native Computer Use pipe works
  or the user supplies explicit visual confirmation.
- `V0413-T01K-DIST-001`: no Developer ID, notarization, public Gatekeeper, or
  cross-machine distribution evidence.
- The 123 legacy calls remain global unattributed unknowns; their session,
  stage, model, tokens, and cost cannot be reconstructed.
- Token safety covers the inspected platform, bridge, standalone daemon, and
  PID-bound built-in runtimes at capture time. It is not an OpenAI/Codex
  account-wide billing monitor.

### Hash and source checks

The reviewer independently matched the r6 contract digest, both final commit
identities, the wheel, sdist, application ZIP, application executable,
`Info.plist`, and `CodeResources` hashes recorded in the distribution
manifest. It also confirmed that behavior-affecting final changes were
followed by the applicable refreshed test sets.
