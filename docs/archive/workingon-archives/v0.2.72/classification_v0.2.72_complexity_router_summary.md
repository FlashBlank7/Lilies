# v0.2.72 complexity-router requirement classification contract

- Raw evidence: `docs/workingon/classification_v0.2.72_complexity_router.json`
- Contract satisfied: `True`
- Default enabled: `False`
- Allowed to enable default: `False`
- Missing prerequisites: `operator_override_plan`, `rollout_metrics_prerequisites`

| Sample | Requirement class | Effective class | Conservative unknown | Signals |
| --- | --- | --- | --- | --- |
| `simple` | `simple` | `simple` | `False` | `fix`, `typo` |
| `medium` | `medium` | `medium` | `False` | `api`, `endpoint`, `report`, `test`, `workflow` |
| `complex` | `complex` | `complex` | `False` | `agent`, `guardrail`, `model-sensitive`, `platform`, `rollout`, `router` |
| `unknown` | `unknown` | `complex` | `True` | `empty_requirement` |

## Conclusion

Requirement classification contract is satisfied and API-visible. Complexity-router defaults remain disabled because operator override plan and rollout metrics prerequisites are still missing.
