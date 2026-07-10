# work_v0.2.61_adaptive_monitoring_refresh_control

## Goal

Add a manual refresh control and persisted freshness history for the adaptive Template monitoring surface.

## Source

- Stage report: `docs/stage-reports/v0.2.60_adaptive_monitoring_product_surface.md`
- Version: `v0.2.61`

## Full Task Set

| Task | Disposition | Design | Reason |
| --- | --- | --- | --- |
| Add scheduled/manual adaptive drift checks | accepted as manual refresh slice | `docs/current-design/design_adaptive_monitoring_refresh_control.md` | v0.2.60 exposed a static snapshot; the smallest next step is a safe manual refresh with persisted history/freshness signal. |
| Preserve deferred lanes | deferred | none | E08 and complexity router remain later candidates after E05 monitoring freshness is product-visible. |

## Execution Status

| Design | Status | Evidence | Next action |
| --- | --- | --- | --- |
| `design_adaptive_monitoring_refresh_control.md` | completed | `docs/workingon-archives/v0.2.61/implementation_v0.2.61_adaptive_monitoring_refresh_control.md`; `tests/test_adaptive_monitoring_product_surface.py` | archive |

## Acceptance

- Backend can record a manual adaptive monitoring refresh event without paid/live runs.
- GET status includes last refresh and recent history.
- Studio adaptive monitoring panel shows last refresh/history and has a manual refresh button.
- Focused backend tests pass.

## Completion Gate

- All tasks dispositioned: yes
- Accepted design completed: yes
- Backend refresh/history verification: passed
- Frontend TypeScript verification: skipped because `node`/`npm` are unavailable in this shell
- Archive ready: yes
