# v0.4.8 Studio, governance, and evaluation evidence lifecycle

Status: active
Source task: `V04-08-T01`
Mandatory tasks: `V04-08-T01D`, `V04-08-T01E`, `V04-08-T01F`, `V04-08-T01G`
Source intents: `EVAL-001`, `EVAL-002`, `EVAL-003`, `EVAL-004`, `GOV-003`

## Authenticated API

- `GET /api/v1/evaluation/profiles`
- `GET /api/v1/evaluation/environments`
- `POST /api/v1/applications/{id}/evaluation/plan`
- `POST /api/v1/applications/{id}/evaluation/tests/apply`
- `POST /api/v1/applications/{id}/evaluation/runs`
- `GET /api/v1/applications/{id}/evaluation/runs`
- `GET /api/v1/evaluation/runs/{run_id}`

All application mutations require revision and content-hash guards. Exact run reads and history are durable. Platform Harness uses `evaluation_run` as a distinct kind and records selected profile/environment, draft identity, execution mode, status, blockers, and achieved claim.

## Engineer Studio

The Test tab gains one compact Evaluation Harness workspace above existing acceptance cards. Engineers select a profile and environment, see availability and mutation boundaries, preview generated capability cases, apply with a guarded action, run eligible evaluation, and inspect achieved status, blockers, verified/excluded claims, per-case evidence, and recent history. Profile selection uses a segmented control; environment uses a labeled select/menu; unavailable states disable execution but remain inspectable. Customer Runtime must contain no Evaluation Harness controls or evidence internals.

## Governance and evidence

Governance task search and trace include `evaluation_run` tasks without a special parallel store. Enroll `platform.evaluation_harness_profiles` in the Capability Evidence Registry with implementation, API, automated-test, integration, and known-gap artifacts. The claim ceiling is local H3 until eligible H4/H5 evidence exists. The console may show exact achieved outcomes, but it must not turn configured profile labels or blocked runs into verified claims.

## Closure

Verify profile and environment semantics, generator diversity, safe apply, status ceilings, restart persistence, Platform Harness linkage, Studio desktop/mobile operation, Customer Runtime non-disclosure, Governance visibility, production build, current gate, full historical diagnostic, and contract-first closure. Browser evidence must include one local eligible run and one unavailable higher-profile plan.
