# v0.4.3 Evidence And Publication Lifecycle

Status: active

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

