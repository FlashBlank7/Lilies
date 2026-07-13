# implementation_v037_draft_detail_first_run_guidance

状态：completed

## Source

- Source stage report: `docs/stage-reports/v0.3.6_runtime_product_health_triage.md`
- Active designs:
  - `docs/current-design/design_v037_detail_first_run_orientation.md`
  - `docs/current-design/design_v037_draft_next_action_checklist.md`
  - `docs/current-design/design_v037_detail_guidance_persona_harness.md`

## Work Completed

- Added first-run orientation markers to the application detail canvas guidance:
  - `data-detail-guidance="first-run-orientation"`
  - structure, acceptance, try-run, and monitor signals
- Added a next-action checklist:
  - `data-detail-guidance="next-action-checklist"`
  - `data-next-action` markers for inspect, build, test, run, publish, and monitor
  - each item switches to an existing studio tab
- Added Chinese and English copy for the new detail guidance.
- Added CSS for the detail signal grid and next-action checklist.
- Added v0.3.7 detail guidance persona harness and focused tests.
- Strengthened live evidence to verify rendered detail HTML contains the new guidance markers.

## Evidence

- Live evidence file: `docs/workingon/detail_guidance_persona_v0.3.7.json`
- Focused tests: `.venv/bin/python -m pytest tests/test_v03_7_detail_guidance_persona.py -q`
- Cross-version regression: `.venv/bin/python -m pytest tests/test_v03_7_detail_guidance_persona.py tests/test_v03_6_runtime_health_identity.py tests/test_v03_6_runtime_persona_triage.py tests/test_v03_5_smoke_cleanup_boundary.py tests/test_v03_4_browser_unavailable_smoke_retention.py tests/test_v03_3_safe_draft_skeleton_flow.py tests/test_v03_2_bounded_create_open_detail_flow.py tests/test_v03_1_customer_flow_blackbox_audit.py tests/test_v03_0_usability_customer_journey_audit.py tests/test_stage_report_template_validation.py -q`

## Results

- Focused tests: `5 passed`.
- Cross-version regression: `38 passed`.
- Live evidence: passed.
- Rendered detail guidance markers: passed.
- No-build safety: passed.
- Smoke cleanup: passed.

## Verification Limitations

- `npm run lint` could not run in this shell because `npm` and `node` are not on PATH.
- Existing Next dev server still rendered the updated detail HTML, so the stage used rendered route evidence plus source marker tests instead of claiming TypeScript compile verification.
