# v0.4.3 Evidence And Publication Lifecycle

Status: active - implementation complete, browser evidence pending

## Contract

- Task: `V04-03-T01C`
- Intents: `PRODUCT-004`, `PRODUCT-007`

## Problem

Every draft edit currently sets `tested_hash` to null and erases the validation report. Publication then rejects every missing or stale hash. The user loses useful prior evidence, cannot see what became stale, and cannot exercise the report's Quick or Guided advisory publication boundary.

## Boundary

- Behavior-affecting edits invalidate freshness, not history.
- Quick and Guided show warnings and retain a deliberate publish action.
- Governed blocks only when `hard_publish_gate` is explicitly enabled.
- Published versions capture the evidence state and warnings that supported the user's decision.

## Design

- Preserve `last_tested_hash` and validation report when content changes; derive `evidence_state` as `current`, `stale`, or `missing`.
- Record invalidation revision, timestamp, and a bounded summary of behavior-affecting operations.
- Add a publication decision object: `allowed`, `requires_confirmation`, `blocked`, warning codes, evidence state, and policy source.
- Make publish a two-step API when confirmation is required: preview policy, then send explicit acknowledgement.
- Show stale/current/missing evidence near the primary publish command with revalidate and inspect-evidence actions.
- Keep restore and version history semantically complete by recording the publication decision with the version.

## Acceptance

- Editing a tested draft leaves prior evidence visible but stale.
- Quick/Guided publish succeeds only after explicit warning acknowledgement.
- Governed with hard gate rejects stale or missing evidence with a structured reason; without the flag it remains advisory.
- Tests cover current, stale, missing, restore, and concurrent-edit cases.

## Implementation Result

- Draft edits retain `tested_hash` and the last validation report, while evidence derives to `current`, `stale`, or `missing`.
- Stale evidence includes invalidation time, revision, a bounded operation summary, and a revalidation endpoint.
- `GET .../publication-decision` returns allowed, confirmation, blocked, warning codes, evidence, policy, and policy source.
- Quick, Guided, and advisory Governed publication require explicit warning acknowledgement; Governed blocks only with the persisted hard-gate flag.
- Published versions retain the exact publication decision. Restoring a version restores the evidence state captured with that version and rejects concurrent draft changes.
- Studio shows evidence state beside Publish, offers revalidate/inspect actions, and presents explicit confirmation or hard-block UI. The application list no longer treats stale evidence as ready.

## Verification Evidence

- `.venv/bin/python -m pytest tests/test_v04_03_evidence_publish_lifecycle.py tests/test_v04_03_delivery_modes.py -q` -> `11 passed, 1 warning`.
- `.venv/bin/python -m pytest tests/test_workflow.py -q` -> `77 passed, 1 warning`.
- Current v0.4.x gate -> `31 passed, 1 warning`.
- Full suite -> `715 passed, 17 xfailed, 1 warning`; final inventory has zero blocking, unknown, or missing conflicts.
- Frontend TypeScript compilation and production build pass under the verified local Node toolchain.
- Rendered Studio interaction remains unverified because the supported Browser runtime exposes no browser; product acceptance stays open under `V04-03-T01F`.
