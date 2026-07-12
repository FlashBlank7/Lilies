# implementation_v035_smoke_cleanup_boundary

状态：completed

## Source

- Source stage report: `docs/stage-reports/v0.3.4_browser_flow_and_smoke_retention.md`
- Active designs:
  - `docs/current-design/design_v035_smoke_cleanup_api_boundary.md`
  - `docs/current-design/design_v035_cleanup_retention_harness.md`

## Work Completed

- Added a bounded smoke cleanup API:
  - `POST /api/v1/applications/{application_id}/smoke-cleanup`
  - requires bearer token
  - accepts only `v0.3.<n>-smoke` markers
  - defaults to `dry_run: true`
  - rejects applications whose name, description, or requirement do not contain the marker
- Added storage cleanup that reports related row counts before deletion.
- Added focused API tests for dry-run, delete, and non-smoke rejection.
- Added a live evidence script that creates one `v0.3.5-smoke` app, dry-runs cleanup, deletes it, verifies it is gone, and records that no build endpoint was called.

## Evidence

- Live evidence file: `docs/workingon/smoke_cleanup_boundary_v0.3.5.json`
- Live result: passed
- Build/model boundary: no `/builds` endpoint called
- Focused test command already passed during implementation:
  - `.venv/bin/python -m pytest tests/test_v03_5_smoke_cleanup_boundary.py -q`

## Notes

- This is intentionally not a general application delete endpoint.
- Existing non-smoke customer applications remain outside this cleanup boundary.
- Broader product data-management UX can be designed in a future stage once non-technical user flows are clearer.
