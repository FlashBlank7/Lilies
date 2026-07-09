# implementation_platform_harness_network_egress_policy_2026_07_09

## Summary

`v0.2.16` added Platform Harness network egress policy enforcement for WorkflowRuntime HTTP blocks.

## Code Changes

- `platform/backend/src/agent_platform/config.py`
  - Added `platform_harness_network_egress_policy`.
  - Added `platform_harness_network_egress_allowlist`.
- `platform/backend/src/agent_platform/api.py`
  - Injected egress policy config into `PlatformHarness`.
- `platform/backend/src/agent_platform/platform_harness.py`
  - Added `enforce_network_egress_policy()`.
  - Supports `full`, `none`, and hostname `allowlist`.
- `platform/backend/src/agent_platform/workflow_runtime.py`
  - Checks egress policy before HTTP block external request.
- `tests/test_workflow.py`
  - Added `test_platform_harness_network_egress_policy_blocks_http_requests`.

## Verification

Focused:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_network_egress_policy_blocks_http_requests tests/test_workflow.py::test_platform_harness_secret_policy_blocks_http_secret_headers -q
```

Result:

- `2 passed, 1 warning`

Full:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/platform_harness.py platform/backend/src/agent_platform/workflow_runtime.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `63 passed, 1 warning`

## Paid/Live Test Decision

Skipped intentionally. This stage is deterministic policy enforcement and does not depend on model/provider behavior.

## Remaining Risk

- WebSearch, MCP, and sandbox/container egress are not yet covered by this policy.
- Hostname allowlist is not equivalent to DNS/IP firewall enforcement.

