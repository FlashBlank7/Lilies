# v0.2.48 E05 adaptive reuse policy backtest

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.48_e05_adaptive_reuse_policy_backtest_2026_07_10.json`
- Policy mode: `adaptive`
- Exact matches: `2`
- Bounded matches: `1`

## Families

| Family | Top template | Policy | Action | Best known | Alignment | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| code_reviewer | code_reviewer | shallow | expand_template | shallow | exact_match | experiment-status/evidence/experiment_v0.2.41_e05_success_condition_2026_07_09_summary.md |
| customer_support_router | customer_support_router | shallow | expand_template | mixed | within_success_envelope | experiment-status/evidence/experiment_v0.2.46_e05_customer_support_deep_only_teammate_governance_2026_07_10_summary.md |
| data_analyzer | data_analyzer | deep | compose_modules | deep | exact_match | experiment-status/evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10_summary.md |

## Notes

- `code_reviewer`: Existing E05 evidence favors shallow over deep for the code-review family. Reason=`adaptive:template_match:code_reviewer`.
- `customer_support_router`: Customer-support evidence is mixed across governance slices, so adaptive should stay conservative instead of forcing deep by default. Reason=`adaptive:template_match:customer_support_router`.
- `data_analyzer`: The latest breadth/default slice showed deep publishing while shallow timed out. Reason=`adaptive:complex_blocks:parameter_extractor`.

