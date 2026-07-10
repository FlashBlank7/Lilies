# implementation_v0.2.61_adaptive_monitoring_refresh_control

## Goal

Add manual refresh and persisted freshness history to the adaptive Template monitoring surface.

## Changes

- Extended `agent_platform.adaptive_monitoring` with append-only JSONL refresh history in runtime `data_dir`.
- Added `record_adaptive_monitoring_refresh` and status-with-history helpers.
- Added authenticated `POST /api/v1/templates/adaptive-monitoring/refresh`.
- Extended `GET /api/v1/templates/adaptive-monitoring` to include `last_refresh`, `history`, `history_count`, and `history_path`.
- Added backend regression coverage for persisted refresh history.
- Added Studio refresh button plus last-refresh/history display.

## Verification

| Check | Result | Command |
| --- | --- | --- |
| Backend refresh/history regression | `3 passed, 1 warning` | `./.venv/bin/python -m pytest tests/test_adaptive_monitoring_product_surface.py -q` |
| Static refresh reference check | passed | `rg -n "recordAdaptiveMonitoringRefresh|last_refresh|history_count|adaptiveRefresh|adaptive_template_policy_history|adaptive-monitoring/refresh|record_adaptive_monitoring_refresh" platform/backend platform/frontend tests/test_adaptive_monitoring_product_surface.py` |
| Frontend TypeScript check | skipped | `npm run lint` could not run because `npm`/`node` are not available in this shell. |

## API Result

- POST refresh appends a JSONL record under `data_dir/monitoring/adaptive_template_policy_history.jsonl`.
- GET after refresh returns `history_count=1` and a non-null `last_refresh`.
- Refresh rechecks current deterministic evidence only; it does not start paid/live model calls.

## Remaining Risk

- This is a manual refresh control, not a scheduled background drift checker.
- Frontend TypeScript/build verification still needs a machine with `node`/`npm` available.
