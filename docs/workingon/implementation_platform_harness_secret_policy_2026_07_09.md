# implementation_platform_harness_secret_policy_2026_07_09

## Summary

`v0.2.15` added Platform Harness secret policy enforcement for explicit secret fields in AgentRuntime tool calls, WorkflowRuntime Tool blocks, and WorkflowRuntime HTTP blocks.

## Code Changes

- `platform/backend/src/agent_platform/platform_harness.py`
  - Added secret field markers.
  - Added `enforce_secret_policy()`.
- `platform/backend/src/agent_platform/config.py`
  - Added `platform_harness_secret_policy_enabled`.
- `platform/backend/src/agent_platform/api.py`
  - Creates PlatformHarness before AgentRuntime and injects it into AgentRuntime.
- `platform/backend/src/agent_platform/runtime.py`
  - Checks tool input before permission flow and tool events.
- `platform/backend/src/agent_platform/workflow_runtime.py`
  - Checks Tool block input and HTTP headers/query/body before external execution.
- `tests/test_workflow.py`
  - Added HTTP header block regression.
- `tests/test_runtime.py`, `tests/test_factory.py`
  - Updated manual AgentRuntime construction with PlatformHarness.

## Verification

Focused:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_secret_policy_blocks_http_secret_headers tests/test_runtime.py::test_runtime_executes_tool_loop_and_persists_events tests/test_factory.py::test_factory_generates_valid_platform_agent -q
```

Result:

- `3 passed, 1 warning`

Full:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/platform_harness.py platform/backend/src/agent_platform/runtime.py platform/backend/src/agent_platform/workflow_runtime.py tests/test_workflow.py tests/test_runtime.py tests/test_factory.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `62 passed, 1 warning`

## Paid/Live Test Decision

Skipped intentionally. The feature is deterministic policy enforcement and does not depend on model/provider behavior.

## Remaining Risk

- No secret store exists yet.
- Secrets embedded inside arbitrary command strings are not detected.
- Network egress policy remains separate future work.

