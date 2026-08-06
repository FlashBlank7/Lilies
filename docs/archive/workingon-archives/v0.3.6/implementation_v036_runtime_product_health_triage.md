# implementation_v036_runtime_product_health_triage

状态：completed

## Source

- Source stage report: `docs/stage-report-archives/v0.3.x/v0.3.5_smoke_archive_cleanup_boundary.md`
- Active designs:
  - `docs/current-design/design_v036_runtime_current_code_health.md`
  - `docs/current-design/design_v036_customer_persona_blackbox_triage.md`
  - `docs/current-design/design_v036_p0p1_runtime_usability_fix.md`

## Work Completed

- Added backend runtime identity:
  - FastAPI app version now uses `agent_platform.__version__`.
  - `/health` reports `runtime.version`, `product_phase`, git branch/commit, `tracked_dirty`, `untracked_present`, selected route availability, and `current_code_ready`.
  - Selected route checks include the v0.3.5 smoke cleanup endpoint.
- Reconciled the local runtime:
  - stopped stale backend on `127.0.0.1:8001`,
  - restarted `8001` from the current `usabilityEnhence` branch,
  - verified both backend `/health` and frontend proxy `/api/platform/health` report `v0.3.6`.
- Added customer persona triage:
  - confused first-time business owner checks the home route and customer/safe-draft source affordances,
  - concrete workflow builder creates and seeds a no-build `v0.3.6-smoke` draft,
  - returning draft reviewer lists apps, opens the draft, and checks the detail route.
- Used the v0.3.5 cleanup boundary to delete the v0.3.6 smoke artifact.

## Evidence

- Live evidence file: `docs/workingon/runtime_persona_triage_v0.3.6.json`
- Focused tests: `.venv/bin/python -m pytest tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py -q`
- Cross-version regression: `.venv/bin/python -m pytest tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py tests/test_v03_5_smoke_cleanup_boundary.py tests/test_v03_4_browser_unavailable_smoke_retention.py tests/test_v03_3_safe_draft_skeleton_flow.py tests/test_v03_2_bounded_create_open_detail_flow.py tests/test_v03_1_customer_flow_blackbox_audit.py tests/test_v03_0_usability_customer_journey_audit.py tests/test_stage_report_template_validation.py -q`

## Results

- Focused tests: `7 passed`.
- Cross-version regression: `33 passed`.
- Live evidence: passed.
- No-build safety: passed; no `/builds` endpoint was called.
- Smoke cleanup: passed; `verified_smoke_deleted` returned 404.
- P0/P1 ledger: no open blocking P0/P1.

## Notes

- Runtime git identity intentionally reports dirty/untracked state. During this stage it reports dirty because v0.3.6 changes are still uncommitted and `.tmp/` / `uv.lock` remain untracked.
- The health surface is diagnostic only and does not expose tokens or environment variable values.
