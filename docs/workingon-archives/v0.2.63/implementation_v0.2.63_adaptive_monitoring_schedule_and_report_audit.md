# implementation_v0.2.63_adaptive_monitoring_schedule_and_report_audit

## Source

- Source stage report: `docs/stage-reports/v0.2.62_evolution_process_architecture.md`
- Source stage task: `Add scheduled adaptive drift check hook`; `Audit recent stage reports for template migration need`
- Current design: `docs/current-design/design_adaptive_monitoring_scheduled_hook.md`; `docs/current-design/design_stage_report_template_migration_audit.md`

## Changes

- Added trigger-aware adaptive monitoring refresh records.
- Added disabled-by-default `adaptive_monitoring_refresh_interval_seconds`.
- Added FastAPI lifespan hook for a named background refresh loop when the interval is positive.
- Added schedule visibility and deterministic run-once API endpoints.
- Added a stage-report template adoption audit script that reuses the mandatory validator.
- Added tests for schedule status, run-once history, background task startup, and audit recommendation logic.

## Evidence / Intermediate Results

Scheduled adaptive monitoring endpoints:

- `GET /api/v1/templates/adaptive-monitoring/schedule`
- `POST /api/v1/templates/adaptive-monitoring/schedule/run-once`

Template adoption audit command:

```text
.venv/bin/python scripts/audit_stage_report_template_adoption.py --limit 8
```

Result:

```text
Recommendation: `forward_only_keep_historical_reports_as_is`
```

Recent audit conclusion:

- v0.2.55 through v0.2.61 predate the mandatory template and do not conform.
- v0.2.62 conforms.
- No historical rewrite is needed in v0.2.63; future reports must conform.

## Verification

```text
.venv/bin/python -m pytest tests/test_adaptive_monitoring_product_surface.py tests/test_stage_report_template_adoption_audit.py tests/test_stage_report_template_validation.py
```

Result:

```text
10 passed, 1 warning in 0.46s
```

`uv run ...` was attempted first, but `uv` was not available in the shell PATH, so verification used the repository `.venv/bin/python`.

## Remaining Risk

- The scheduled hook is intentionally default-off. Operators must configure `adaptive_monitoring_refresh_interval_seconds` to enable automatic periodic appends.
- The hook records the deterministic existing snapshot; it does not regenerate paid/live monitoring evidence.
- Historical report migration remains intentionally forward-only unless a future stage report selects a migration stage.

## Design Decision

- Continue current design / revise current design / proceed to next design / blocked: proceed to archive
