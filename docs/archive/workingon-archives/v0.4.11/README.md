# v0.4.11 Working Evidence Archive

This archive preserves the closure evidence for the human-journey usability repair completed in implementation commit `7851713768618828a398bc6b14328e6754bf0c72`. The immutable stage contract was established in `7ac4ab594b73d04baf325bb277d5ef7ed36dccfe`.

## Evidence Boundary

- The evidence comes from the local Lilies platform after a clean restart on runtime `v0.4.11`, with real browser sessions, real HTTP requests, persisted application state, and bounded configured-model calls for workflow planning, building, and execution.
- The five modeled journeys cover an acceptance operator, a customer recovering a failed run, a workflow maintainer, a mobile customer running an existing workflow, and a customer moving from one natural-language requirement through Builder publication into Customer Runtime.
- Smoke applications use `v0.4.11-smoke`. Seeded and generated journeys record cleanup where applicable; the paid requirement-to-runtime journey proves deletion of its application, draft, published version, build, runs, and idempotency records.
- The diagnostic snapshot preserves workflow, capability, acceptance, build, and run state but no API token or authorization header.
- The strongest claim is local product behavior and regression stability. This archive does not prove an external customer deployment, production reliability, production security certification, or the absence of every future defect.

## Repaired Product Paths

1. Builder Team now preserves the created application and exact build operation, persists live progress before emitting events, and keeps the resulting application visible after navigation or refresh.
2. Natural-language workflow editing can use the model for whole-workflow operations beyond the narrow deterministic grammar, validates every emitted operation, and treats referenced blocks as context rather than an edit boundary.
3. Selecting blocks no longer crashes the Engineer Studio; block inspection, one-click arrange, WASD panning, right-click reference capture, and readable workflow summaries coexist on the same canvas.
4. Failed acceptance cases can open an actionable repair preview, keep apply actions visible, preserve identifiers such as `permission_gate`, apply the repair, and automatically rerun real tests to a terminal result.
5. Customer Runtime hides internal failed-run errors, places recovery before the fold, focuses and marks missing required input, clears corrected errors, blocks synchronous duplicate starts, and renders successful structured results as readable sections.
6. Mobile Runtime fits a 390 by 844 viewport without horizontal overflow; the start action and final result remain usable and the output no longer exposes serialized JSON syntax.
7. Automation now distinguishes not configured, unpublished draft, and active schedule states; ordinary workflows no longer look operationally broken merely because they have no schedule.
8. Connector operations and governance queries remain application- and tenant-scoped, while empty integration views communicate that setup has not started.
9. Smoke cleanup now accepts strict semantic-version markers such as `v0.4.11-smoke` instead of being permanently hard-coded to the v0.3 line; unversioned markers and nonmatching applications remain protected.
10. AI requirement intake normalizes provider-generated choice actions and requirement axes while preserving the option-first protocol for ambiguous requests; sufficiently specified requests can proceed directly to a capability-linked workflow plan.
11. Builder publication now requires every required capability to have an implementation carrier and mandatory-test coverage, functional outputs to have explicit assertions, customer-facing replies to have negative safety checks, and requested customer-visible traces to have a structured step-log assertion.
12. Customer Runtime renders the complete structured business result rather than only the last field, removes duplicate transport wrappers, localizes stable field aliases, and keeps all generated step-log entries readable.

## Human Journey Results

| Journey | Result | Direct evidence |
| --- | --- | --- |
| Acceptance failure to automatic repair | pass | Initial real test failed, repair preview was actionable, apply triggered exactly one automatic rerun, both `/tests/run` requests returned 200, final status was `通过`, and the fixture was deleted. |
| Failed Customer Runtime recovery | pass | Raw internal error was hidden, empty retry sent zero requests and focused an `aria-invalid` field, a synchronous double activation sent one request, the replacement run succeeded 1/1, and the fixture was deleted. |
| Maintainer Engineer Studio | pass | Six blocks opened stable inspectors, WASD changed the viewport, reference count was one, the whole-workflow preview contained exactly the requested rename and description operations, operational empty states were truthful, and the application remained visible on the homepage. |
| Mobile customer run | pass | The 390-pixel viewport had no horizontal overflow, the start action was visible, the run succeeded 3/3, and the result rendered Chinese classification sections without raw `"classification":` JSON. |
| Paid requirement to Builder to Runtime | pass | A specific support-workflow request became a capability-linked plan, Builder published a three-node editable workflow with one mandatory acceptance test and no repair cycle, Customer Runtime succeeded with six business sections plus five step-log entries, all hard content/layout checks passed, and cleanup deleted every smoke record. |

`browser/browser-evidence.json` is the machine-readable summary. The five complete journey JSON files retain transitions, request counts, accessibility state, cleanup results, screenshots, console errors, and failed requests. All five journeys report no product error, no console error, and no failed request.

## Regression And Static Verification

- Current release gate: `159 passed`, `0 failed`, one existing Starlette/httpx deprecation warning. See `current-release-gate.json`.
- Full repository test sweep: `771 passed`, `85` strict archived-expectation `xfail`, `0 failed`, `0 errors`, one warning. The classifier found no current regression, unknown expected conflict, or missing expected conflict. See `full-suite-classification.json`.
- Frontend: TypeScript lint and the Next.js 16.2.9 production build passed.
- Modified Python scope: targeted Ruff passed for all 27 Python files changed between the contract baseline and implementation commit.
- Browser harnesses: all four Node scripts passed `node --check`; `git diff --check` passed.
- Whole-repository Ruff remains diagnostic-only and is not green: `397` findings exist across legacy demo scripts, historical tests, and unrelated old modules. See `full-ruff-diagnostic.json`. This stage makes no global Ruff-clean claim and does not hide these findings behind the modified-file result.
- Restart: `runtime-health.json` records backend status `ok`, runtime `v0.4.11`, implementation commit `7851713`, product phase `v0.4.x`, `current_code_ready=true`, and frontend HTTP 200.

## Artifact Integrity

`SHA256SUMS` contains SHA-256 digests for every archived JSON and PNG artifact except `README.md` and the checksum file itself. Verify from this directory with:

```bash
shasum -a 256 -c SHA256SUMS
```

The authoritative task closure, deviations, intent coverage, and claim limits are recorded in `docs/stage-reports/v0.4.11_human_journey_usability_repair.md`.
