# stage-reports

This directory is the active small-version stage-report workspace for the current major phase.

Current state:

- Active phase: `v0.3.x` is active.
- Active stage reports: `v0.3.0_product_usability_stabilization.md`, `v0.3.1_customer_requirement_intake_and_blackbox_flow.md`, `v0.3.2_bounded_create_open_detail_flow.md`.
- Template: `STAGE_REPORT_TEMPLATE.md`.
- Latest handoff source: `docs/stage-report-archives/v0.2.x/v0.2.144_v02x_closeout_and_v03_handoff.md`.
- Latest completed version: `v0.3.2_bounded_create_open_detail_flow`.
- Next planned version: `v0.3.3_safe_draft_starter_skeleton_and_cleanup`.
- Current target: make safe draft applications useful after opening by adding a non-model starter skeleton and deciding cleanup/archive behavior for smoke apps.

Rules:

- New `v0.3.x` stage reports belong here until `v0.3.x` is completed.
- Completed major-version stage reports must be moved into `docs/stage-report-archives/v0.<minor>.x/`.
- If this directory has no active report yet, the next task source is the latest archived handoff stage report plus the matching phase report.
- Do not put phase closeout summaries here; use `docs/phase-reports/`.
