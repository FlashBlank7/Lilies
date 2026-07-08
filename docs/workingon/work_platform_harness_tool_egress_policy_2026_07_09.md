# work_platform_harness_tool_egress_policy_2026_07_09

## Goal

推进 `v0.2.17_platform_harness_tool_egress_policy`：把 WebSearch / HTTP MCP 这类工具级网络出站纳入 Platform Harness network egress policy。

## Scope

包含：

- AgentRuntime WebSearch / HTTP MCP egress policy check。
- WorkflowRuntime Tool block WebSearch / HTTP MCP egress policy check。
- 回归测试：WebSearch Tool block 在 `none` egress policy 下被阻断。

不包含：

- sandbox/container 防火墙。
- stdio MCP 内部网络审计。
- DNS/IP 级 enforcement。
- 前端 UI。

## Linked Current Design

- `docs/current-design/design_platform_harness_tool_egress_policy_v1.md`

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Audit v0.2.16 design archive gate | completed |
| 2 | Add AgentRuntime tool egress checks | completed |
| 3 | Add WorkflowRuntime Tool block egress checks | completed |
| 4 | Add WebSearch Tool block regression test | completed |
| 5 | Run focused and full verification | completed |
| 6 | Archive v0.2.17 with design recycling | completed |

## Acceptance Criteria

- WebSearch tool calls are blocked when platform egress policy is `none`.
- HTTP MCP calls can be checked against host allowlist when the agent has an HTTP MCP server URL.
- Existing HTTP block egress behavior still passes.
- Full backend tests pass.

## Current Decision

Implement tool-level egress policy for network tools with known target host. Stdio MCP remains out of scope because the child process can perform arbitrary behavior without a known host.

## Implementation Evidence

- `AgentRuntime._enforce_tool_network_policy()`
- `WorkflowRuntime._enforce_tool_network_policy()`
- Regression test: `test_platform_harness_tool_egress_policy_blocks_websearch_tool`

Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_tool_egress_policy_blocks_websearch_tool tests/test_workflow.py::test_platform_harness_network_egress_policy_blocks_http_requests -q
```

Result:

- `2 passed, 1 warning`

Full verification:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
.venv/bin/python -m ruff check platform/backend/src/agent_platform/runtime.py platform/backend/src/agent_platform/workflow_runtime.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `64 passed, 1 warning`

Paid/live model test:

- Not required. This stage is deterministic tool policy enforcement.

