# implementation_v0318_application_list_search_sort

状态：completed

## Source

- Stage report: `docs/stage-report-archives/v0.3.x/v0.3.17_application_list_status_filters.md`
- Accepted next-stage task set:
  - `Start v0.3.18_application_list_search_sort`
  - `Add app-list search`
  - `Add predictable sort`
  - `Extend app-list harness`
  - `Preserve regression lane manifest`

## Implementation

- Added application-list search input that filters the status-filtered app set by application name and description.
- Added a compact sort select with readiness-first, revision-first, and name ordering.
- Preserved the existing readiness filter semantics by layering `statusFilteredApps -> searchedApps -> visibleApps`.
- Added bilingual search/sort copy and compact responsive styling.
- Added deterministic fixture evidence for search and all three sort modes.
- Updated the current v0.3.x release gate manifest to include v0.3.18.
- Repaired the v0.3.17 source marker so the older filter check follows the renamed semantic layer `statusFilteredApps` instead of the old local variable name.

## Verification

- Focused compatibility: `.venv/bin/python -m pytest tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_10_frontend_verification_recovery.py -q`
- Result: `26 passed`.
- Live no-build evidence: `.venv/bin/python scripts/v03_18_application_list_search_sort.py --live --api-url http://127.0.0.1:8001`
- Result: passed; endpoint ledger contains only `GET /health`.
- Current release gate: `.venv/bin/python -m pytest tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_14_monitor_trace_readability.py tests/test_v03_13_acceptance_publish_guidance.py tests/test_v03_12_canvas_node_inspector_guidance.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py tests/test_v03_9_build_action_guard.py tests/test_v03_8_runtime_connection_status.py tests/test_v03_7_detail_guidance_persona.py tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py tests/test_v03_5_smoke_cleanup_boundary.py tests/test_v03_4_browser_unavailable_smoke_retention.py tests/test_v03_3_safe_draft_skeleton_flow.py tests/test_v03_2_bounded_create_open_detail_flow.py tests/test_v03_1_customer_flow_blackbox_audit.py tests/test_v03_0_usability_customer_journey_audit.py tests/test_stage_report_template_validation.py -q`
- Result: `88 passed, 1 warning`.

## Notes

- No backend endpoint changes.
- No browser, build, workflow run, model call, or npm/TypeScript check was executed because this environment still lacks frontend toolchain access.
