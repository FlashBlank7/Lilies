# implementation_platform_harness_tool_egress_policy_2026_07_09

## Summary

`v0.2.17` extended Platform Harness network egress policy to network-capable tools with known target hosts.

## Code Changes

- `platform/backend/src/agent_platform/runtime.py`
  - Added `_enforce_tool_network_policy()` for AgentRuntime tool calls.
  - WebSearch maps to `news.google.com`.
  - HTTP MCP maps to the configured MCP server hostname.
- `platform/backend/src/agent_platform/workflow_runtime.py`
  - Added equivalent Tool block enforcement.
- `tests/test_workflow.py`
  - Added `test_platform_harness_tool_egress_policy_blocks_websearch_tool`.

## Verification

Focused:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_tool_egress_policy_blocks_websearch_tool tests/test_workflow.py::test_platform_harness_network_egress_policy_blocks_http_requests -q
```

Result:

- `2 passed, 1 warning`

Full:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
.venv/bin/python -m ruff check platform/backend/src/agent_platform/runtime.py platform/backend/src/agent_platform/workflow_runtime.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `64 passed, 1 warning`

## Paid/Live Test Decision

Skipped intentionally. This stage is deterministic egress policy enforcement.

## Remaining Risk

- Stdio MCP and sandbox/container egress are not yet enforced by Platform Harness.
- WebSearch host is implementation-specific.

