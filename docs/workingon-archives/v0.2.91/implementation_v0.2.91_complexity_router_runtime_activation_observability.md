# v0.2.91 implementation evidence: complexity-router runtime activation observability

## Source

- Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.90_complexity_router_runtime_activation_path.md`
- Source tasks:
  - Implement complexity-router runtime activation rollout observability
  - Preserve rollback-to-disabled and conservative unknown handling
  - Maintain executable frontend verification evidence

## Completed

- Added recent-build storage query for rollout metrics.
- Added `runtime_activation_rollout_metrics()` to summarize persisted build activation state.
- Added read-only API endpoint `/api/v1/platform/complexity-router/runtime-activation-metrics`.
- Metrics now expose active, bypassed, disabled-default, conservative-unknown, request-override, classification distribution, effective planning mode distribution, runtime reuse-depth distribution, build outcome distribution, rollback value, and sampled records.
- Added deterministic tests for pure metrics and API metrics from persisted build state.
- Generated v0.2.91 metrics evidence and frontend verification evidence.

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| v0.2.91 observability tests | `2 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_v02_91_complexity_router_runtime_activation_observability.py` |
| v0.2.91 plus prior runtime/contract/safety tests | `21 passed, 1 warning` | `.venv/bin/python -m pytest tests/test_v02_91_complexity_router_runtime_activation_observability.py tests/test_v02_90_complexity_router_runtime_activation_path.py tests/test_v02_89_complexity_router_limited_default_enablement_contract.py tests/test_complexity_router_default_safety.py tests/test_stage_report_template_validation.py` |
| Runtime activation observability evidence generation | `completed` | `.venv/bin/python scripts/v02_91_complexity_router_runtime_activation_observability.py` |
| Default metrics | `active=0`; `disabled_default=1` | `docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md` |
| Enabled metrics | `active=2`; `bypassed=1`; `conservative_unknown=1`; `request_override=1` | `docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md` |
| Planning/reuse metrics | planning mode and reuse-depth distributions present | `docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md` |
| Frontend verification | `passed=true` | `docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability.json` |

## Product Boundary

v0.2.91 makes explicit limited-default runtime behavior observable. It does not broaden default enablement; normal settings remain disabled and unknown requirements remain bypassed.
