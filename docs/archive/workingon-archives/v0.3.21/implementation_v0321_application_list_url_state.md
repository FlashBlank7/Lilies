# implementation_v0321_application_list_url_state

状态：completed

## Source

- Stage report: `docs/stage-report-archives/v0.3.x/v0.3.20_detail_tab_url_state.md`
- Accepted next-stage task set:
  - `Start v0.3.21_application_list_url_state`
  - `Add app-list query parser`
  - `Add safe app-list state writer`
  - `Extend app-list URL-state harness`
  - `Preserve regression lane manifest`

## Implementation

- Added app-list query allowlists for `filter`, `q`, and `sort`.
- Added `syncAppListStateFromLocation` to hydrate list controls from URL and browser `popstate`.
- Added `writeAppListUrlState` to update URL state through browser history only.
- Added specific setters for filter, search, and sort:
  - filter and sort use `pushState`.
  - search uses `replaceState` to avoid per-keystroke history spam.
- Removed default URL values for `filter=all`, `sort=readiness`, and empty search.
- Added deterministic parser, writer, direct-setter, source-marker, and no-write safety evidence.
- Updated the current v0.3.x release gate manifest to include v0.3.21.

## Verification

- Focused compatibility: `.venv/bin/python -m pytest tests/test_v03_21_application_list_url_state.py tests/test_v03_20_detail_tab_url_state.py tests/test_v03_19_application_card_quick_actions.py tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_10_frontend_verification_recovery.py -q`
- Result: `44 passed`.
- Live no-write evidence: `.venv/bin/python scripts/v03_21_application_list_url_state.py --live --api-url http://127.0.0.1:8001`
- Result: passed; endpoint ledger contains only `GET /health`.
- Current release gate: `.venv/bin/python -m pytest tests/test_v03_21_application_list_url_state.py tests/test_v03_20_detail_tab_url_state.py tests/test_v03_19_application_card_quick_actions.py tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_14_monitor_trace_readability.py tests/test_v03_13_acceptance_publish_guidance.py tests/test_v03_12_canvas_node_inspector_guidance.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py tests/test_v03_9_build_action_guard.py tests/test_v03_8_runtime_connection_status.py tests/test_v03_7_detail_guidance_persona.py tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py tests/test_v03_5_smoke_cleanup_boundary.py tests/test_v03_4_browser_unavailable_smoke_retention.py tests/test_v03_3_safe_draft_skeleton_flow.py tests/test_v03_2_bounded_create_open_detail_flow.py tests/test_v03_1_customer_flow_blackbox_audit.py tests/test_stage_report_template_validation.py -q`
- Result: `106 passed, 1 warning`.

## Notes

- App-list URL synchronization does not call backend write endpoints.
- Browser and TypeScript/npm verification remain unavailable from this shell environment.
