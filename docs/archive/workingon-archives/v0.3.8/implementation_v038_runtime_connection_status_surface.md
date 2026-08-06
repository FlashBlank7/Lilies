# implementation_v038_runtime_connection_status_surface

状态：completed

## Source

- Source stage report: `docs/stage-report-archives/v0.3.x/v0.3.7_draft_detail_first_run_guidance.md`
- Active designs:
  - `docs/current-design/design_v038_health_derived_status_model.md`
  - `docs/current-design/design_v038_home_runtime_status_surface.md`
  - `docs/current-design/design_v038_detail_runtime_status_surface_and_harness.md`

## Work Completed

- Added `platform/frontend/lib/runtime-status.ts`:
  - `checking`
  - `connected`
  - `auth_required`
  - `stale`
  - `unavailable`
- Replaced the home topbar static environment status with a health/auth-derived runtime badge using `data-runtime-status`.
- Added a compact runtime chip to the application detail header using the same classifier.
- Added Chinese and English runtime status copy.
- Added runtime badge/chip CSS.
- Added v0.3.8 runtime connection status harness and focused tests.

## Evidence

- Live evidence file: `docs/workingon/runtime_connection_status_v0.3.8.json`
- Focused tests: `.venv/bin/python -m pytest tests/test_v03_8_runtime_connection_status.py -q`
- Cross-version regression: `.venv/bin/python -m pytest tests/test_v03_8_runtime_connection_status.py tests/test_v03_7_detail_guidance_persona.py tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py tests/test_v03_5_smoke_cleanup_boundary.py tests/test_v03_4_browser_unavailable_smoke_retention.py tests/test_v03_3_safe_draft_skeleton_flow.py tests/test_v03_2_bounded_create_open_detail_flow.py tests/test_v03_1_customer_flow_blackbox_audit.py tests/test_v03_0_usability_customer_journey_audit.py tests/test_stage_report_template_validation.py -q`

## Results

- Focused tests: `4 passed`.
- Cross-version regression: `42 passed`.
- Live evidence: passed.
- Home runtime marker: passed.
- Detail runtime marker: passed.
- Runtime health: passed.
- Smoke cleanup: passed.
- No-build safety: passed.

## Verification Limitations

- `npm` and `node` are still unavailable on PATH, so TypeScript compile verification could not run in this shell.
- The rendered HTML evidence verifies SSR/default status markers. Hydrated client-state transitions still need Browser or frontend toolchain verification in a later stage.
