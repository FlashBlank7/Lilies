# v0.2.64 productization lane reselection

## Summary

- Raw selection: `docs/workingon-archives/v0.2.64/selection_v0.2.64_productization_lane_reselection.json`
- Winner: `e08_extended_controls`
- Next version: `v0.2.65_e08_policy_controls_surface`
- First design: `docs/current-design/design_e08_policy_controls_surface.md`

## Scores

| Lane | Score | Blocked | Source |
| --- | ---: | --- | --- |
| e08_extended_controls | 16 | False | `docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md` |
| complexity_router_rollout | 6 | False | `docs/experiment-status/ledgers/E07_complexity_router.md` |

## Conclusion

Select E08 extended controls as the next productization lane. Complexity-router remains useful, but its own ledger says it is not default-ready until guardrails and rollout design exist.
