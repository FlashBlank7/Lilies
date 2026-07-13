# implementation_v0320_detail_tab_url_state

状态：completed

## Source

- Stage report: `docs/stage-reports/v0.3.19_application_card_quick_actions.md`
- Accepted next-stage task set:
  - `Start v0.3.20_detail_tab_url_state`
  - `Add safe tab setter`
  - `Preserve build query behavior`
  - `Extend detail navigation harness`
  - `Preserve regression lane manifest`

## Implementation

- Added `setStudioTab` as the detail-page tab setter that updates local tab state and writes `?tab=` through browser history.
- Added `syncStudioTabFromLocation` and a `popstate` listener so browser back/forward can restore valid tabs.
- Preserved existing `?build=` behavior by keeping existing query parameters and using `replaceState` for build-watch tab selection.
- Replaced scattered UI `setTab(...)` calls in node selection, debug draft, panel tabs, run start, build watch, and next-action checklist.
- Added deterministic fixture evidence for query preservation, history method selection, popstate tab guards, direct-setTab guards, and no-write safety.
- Updated the current v0.3.x release gate manifest to include v0.3.20.

## Verification

- Focused compatibility: `.venv/bin/python -m pytest tests/test_v03_20_detail_tab_url_state.py tests/test_v03_19_application_card_quick_actions.py tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_10_frontend_verification_recovery.py -q`
- Result: `38 passed`.
- Live no-write evidence: `.venv/bin/python scripts/v03_20_detail_tab_url_state.py --live --api-url http://127.0.0.1:8001`
- Result: passed; endpoint ledger contains only `GET /health`.
- Current release gate: `.venv/bin/python -m pytest tests/test_v03_20_detail_tab_url_state.py tests/test_v03_19_application_card_quick_actions.py tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_14_monitor_trace_readability.py tests/test_v03_13_acceptance_publish_guidance.py tests/test_v03_12_canvas_node_inspector_guidance.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py tests/test_v03_9_build_action_guard.py tests/test_v03_8_runtime_connection_status.py tests/test_v03_7_detail_guidance_persona.py tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py tests/test_v03_5_smoke_cleanup_boundary.py tests/test_v03_4_browser_unavailable_smoke_retention.py tests/test_v03_3_safe_draft_skeleton_flow.py tests/test_v03_2_bounded_create_open_detail_flow.py tests/test_v03_1_customer_flow_blackbox_audit.py tests/test_v03_0_usability_customer_journey_audit.py tests/test_stage_report_template_validation.py -q`
- Result: `100 passed, 1 warning`.

## Notes

- Tab URL synchronization does not call build, acceptance-run, workflow-run, publish, or restore endpoints.
- Browser and TypeScript/npm verification remain unavailable from this shell environment.
