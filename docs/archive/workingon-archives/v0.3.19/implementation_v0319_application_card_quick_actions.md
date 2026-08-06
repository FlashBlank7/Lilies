# implementation_v0319_application_card_quick_actions

状态：completed

## Source

- Stage report: `docs/stage-report-archives/v0.3.x/v0.3.18_application_list_search_sort.md`
- Accepted next-stage task set:
  - `Start v0.3.19_application_card_quick_actions`
  - `Refactor app card action structure`
  - `Add safe card quick actions`
  - `Extend app-list harness`
  - `Preserve regression lane manifest`

## Implementation

- Refactored home application cards from a single `Link` wrapper into an `article` with a primary detail link and separate quick-action navigation links.
- Added readiness-dependent quick actions:
  - draft apps: edit and acceptance.
  - ready-to-publish apps: acceptance and publish check.
  - published apps: try and monitor.
- Added `?tab=` detail-page deep-link support with a strict `STUDIO_TABS` allowlist.
- Added bilingual quick-action labels and compact action-strip styling.
- Added deterministic quick-action fixture evidence, detail-tab guard evidence, safety evidence, and release-gate manifest coverage.
- Repaired older v0.3.16 style evidence so it checks card guidance semantics rather than a fixed historical card height.

## Verification

- Focused compatibility: `.venv/bin/python -m pytest tests/test_v03_19_application_card_quick_actions.py tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_10_frontend_verification_recovery.py -q`
- Result: `32 passed`.
- Live no-write evidence: `.venv/bin/python scripts/v03_19_application_card_quick_actions.py --live --api-url http://127.0.0.1:8001`
- Result: passed; endpoint ledger contains only `GET /health`.
- Current release gate: `.venv/bin/python -m pytest tests/test_v03_19_application_card_quick_actions.py tests/test_v03_18_application_list_search_sort.py tests/test_v03_17_application_list_status_filters.py tests/test_v03_16_scenario_journey_regression.py tests/test_v03_15_regression_suite_lane_guard.py tests/test_v03_14_monitor_trace_readability.py tests/test_v03_13_acceptance_publish_guidance.py tests/test_v03_12_canvas_node_inspector_guidance.py tests/test_v03_11_guided_try_run_recovery.py tests/test_v03_10_frontend_verification_recovery.py tests/test_v03_9_build_action_guard.py tests/test_v03_8_runtime_connection_status.py tests/test_v03_7_detail_guidance_persona.py tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py tests/test_v03_5_smoke_cleanup_boundary.py tests/test_v03_4_browser_unavailable_smoke_retention.py tests/test_v03_3_safe_draft_skeleton_flow.py tests/test_v03_2_bounded_create_open_detail_flow.py tests/test_v03_1_customer_flow_blackbox_audit.py tests/test_v03_0_usability_customer_journey_audit.py tests/test_stage_report_template_validation.py -q`
- Result: `94 passed, 1 warning`.

## Notes

- Home-card quick actions are navigation-only and do not call build, acceptance-run, workflow-run, publish, or restore endpoints.
- Browser and TypeScript/npm verification remain unavailable from this shell environment.
