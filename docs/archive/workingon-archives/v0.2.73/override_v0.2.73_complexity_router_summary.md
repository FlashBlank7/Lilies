# v0.2.73 complexity-router operator override plan

- Raw evidence: `docs/workingon/override_v0.2.73_complexity_router.json`
- Override plan satisfied: `True`
- Default enabled: `False`
- Allowed to enable default: `False`
- Missing prerequisites: `rollout_metrics_prerequisites`

| Mode | Valid | Target class | Error | Operator-visible reason |
| --- | --- | --- | --- | --- |
| `disabled` | `True` | `None` | `None` | none |
| `force_simple` | `True` | `simple` | `None` | Low-risk text-only edit |
| `force_medium` | `False` | `medium` | `operator_visible_reason_required` | none |
| `force_magic` | `False` | `None` | `unsupported_override_mode` | unsupported mode smoke test |

## Conclusion

Operator override plan is satisfied and API-visible. Complexity-router defaults remain disabled because rollout metrics prerequisites are still missing.
