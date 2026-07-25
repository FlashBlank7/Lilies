# V04-13-T01G Closure Audit 1

Verdict: **FAIL**

Audit context: fresh, read-only reviewer using only the Program Charter,
current stage report and locked Stage Contract, relevant T01G working-tree
changes, and `docs/evidence/v0.4.13/t01g/`.

## Blocking finding

The fresh 19-file changed-scope run produced `1 failed, 206 passed`. Under
`max_tool_calls=3`, twenty concurrent reservations produced three successes,
sixteen `CollaborativeDevelopmentBudgetExceeded` results, and one raw
`FileNotFoundError`, instead of the required three plus seventeen.

Repeated isolation reproduced the anomaly. The race was in
`CollaborativeDevelopmentStore._enforce_storage_permissions()`: a SQLite WAL
or SHM sidecar could disappear between `exists()` and `stat()`/`chmod()`.
Budget did not overrun, but the atomic and restart-safe API leaked an unrelated
filesystem race, so deterministic completion evidence was not reproducible.

## Non-blocking findings

- Q01-Q28 structure, order, strict model, and bundle digest validated.
- All 28 mandatory cases were recorded passed with no mandatory xfail.
- Reconnect, idempotency, lease, and concurrency each retained 100 actual
  iteration records with zero prohibited counters.
- Ordinary/formal non-disclosure, role boundaries, manual/autonomous
  lifecycles, standalone API/CLI, bounded live handoff, and
  `enterprise_denominator=false` were supported by retained evidence.
- Browser evidence was truthfully `blocked_by_environment` and did not claim a
  rendered browser pass.
- Core JSON evidence matched source revision
  `sha256:6c5124ad0d69aab8022b3d5a694b61d70449ead6c1138a40cd9c1ea1607d0e6d`
  at the time of Audit 1.

## Required repair

1. Make SQLite sidecar permission tightening safe against disappearance.
2. Add and repeatedly run a focused concurrency regression.
3. Regenerate every source-bound evidence file and obtain a new fresh-context
   Closure Audit.
