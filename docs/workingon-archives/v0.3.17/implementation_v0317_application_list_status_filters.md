# implementation_v0317_application_list_status_filters

## Summary

v0.3.17 made the home application list filterable by readiness state.

## Completed Work

| Area | Change | Evidence |
| --- | --- | --- |
| Filter UI | Added all, needs acceptance, ready to publish, and published filters. | `data-app-list-filter="status"` |
| Filter logic | Derived status from `tested_hash` and `active_version`. | `appReadinessState` |
| Empty state | Added filtered-empty copy. | `appFilterEmpty` |
| Harness | Added fixture behavior checks and no-build live evidence. | `scripts/v03_17_application_list_status_filters.py` |
| Regression gate | Updated current v0.3.x gate to include v0.3.17. | `pass_count: 83` |

## Verification

| Check | Result |
| --- | --- |
| Focused v0.3.17/v0.3.16/v0.3.15/v0.3.10 tests | `21 passed` |
| Live app-list filter evidence | passed; only `GET /health` |
| Current v0.3.x release gate | `83 passed, 1 warning` |
| Diff whitespace check | passed |

## Notes

- The filter behavior is covered with deterministic fixture apps for draft, ready-to-publish, and published states.
- The change uses application-list fields only and adds no backend endpoint.
