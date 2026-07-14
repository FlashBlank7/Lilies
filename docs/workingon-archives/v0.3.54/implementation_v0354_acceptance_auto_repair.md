# v0.3.54 Acceptance Auto-Repair Implementation

- Source stage report: `docs/stage-reports/v0.3.53_markdown_result_renderer.md`
- User-triggered task: acceptance gate failures showed missing required brick types and no automatic repair path.
- Closure: deterministic repair preview plus confirmed draft application path.

## Completed

| Area | Result | Evidence |
| --- | --- | --- |
| Backend repair preview | Added `AcceptanceRepairPreviewer` and `/api/v1/applications/{application_id}/tests/repair-preview`. | `platform/backend/src/agent_platform/acceptance_repair.py`; `platform/backend/src/agent_platform/api.py` |
| Safe structural repair | Missing safe architecture blocks are inserted in a reachable chain from the existing start node. | `SAFE_REPAIR_ORDER`; focused TestClient flow |
| Safety boundary repair | `permission_gate` and `sandbox_boundary` are inserted with deterministic config; sandbox network policy is explicit. | `acceptance_auto_repair_v0.3.54.json` |
| Output assertion repair | Existing `end` terminal is converted to `answer` when answer is required, avoiding multiple terminal output grouping. | `tests/test_v03_54_acceptance_auto_repair.py` |
| Unsafe behavior boundary | `model_turn`, `tool_executor`, `subagent_spawn`, `tool`, `claude_agent`, and `http_request` are warned/deferred instead of fabricated. | `test_v03_54_backend_defers_unsafe_required_node_types` |
| Frontend repair flow | Failed acceptance runs automatically request a repair preview; application requires a visible confirmation button and reuses draft mutation. | `data-acceptance-repair="failed-gate-preview"` |
| Release gate | Current v0.3.x gate now includes v0.3.54 and expects 315 tests. | `docs/testing/regression_lanes.json` |

## Verification

| Command | Result |
| --- | --- |
| `.venv/bin/python -m pytest tests/test_v03_54_acceptance_auto_repair.py -q` | `5 passed, 1 warning` |
| `.venv/bin/python -m pytest tests/test_v03_54_acceptance_auto_repair.py tests/test_v03_53_markdown_result_renderer.py -q` | `11 passed, 1 warning` |
| `.venv/bin/python scripts/v03_54_acceptance_auto_repair.py --output docs/workingon-archives/v0.3.54/acceptance_auto_repair_v0.3.54.json` | `status=passed` |
| `PATH="/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint` in `platform/frontend` | pass |
| Current v0.3.x release gate from `docs/testing/regression_lanes.json` | `315 passed, 1 warning` |

## Notes

- The real failing path in the current backend can stop at draft validation before individual test runs. The repair preview intentionally reads the current draft and acceptance definitions directly, so it works even when `tests` is empty in the failed report.
- Repair preview does not mutate the draft. The frontend applies returned operations only after user confirmation.
- Existing v0.3.53 manifest checks were updated to accept v0.3.53-or-later manifests while still requiring the v0.3.53 test to remain in the current gate.
