# implementation_v0316_scenario_journey_regression

## Summary

v0.3.16 expanded customer-behavior simulation and fixed a concrete P1 usability gap on the application list.

## Completed Work

| Area | Change | Evidence |
| --- | --- | --- |
| Scenario matrix | Added five-role matrix including investor/demo reviewer. | `scenario_matrix_roles` |
| Application cards | Added draft, acceptance, publish readiness chips. | `data-app-card-guidance="readiness"` |
| Next action | Added card-level next-action guidance for acceptance, publish, and try/monitor paths. | `data-app-card-guidance="next-action"` |
| Home scenarios | Added investor/demo reviewer scenario copy. | i18n markers |
| Regression gate | Updated current v0.3.x gate to include v0.3.16. | `docs/testing/regression_lanes.json` |

## Verification

| Check | Result |
| --- | --- |
| Focused v0.3.16/v0.3.15/v0.3.10 tests | `16 passed` |
| Live scenario journey evidence | passed; only `GET /health` |
| Current v0.3.x release gate | `78 passed, 1 warning` |
| Diff whitespace check | passed |

## Notes

- The selected P1 gap came from the investor/demo reviewer path: the application list previously required users to interpret revision/version metadata instead of seeing readiness and next action.
- The change uses existing application-list fields only; no backend endpoint was added.
