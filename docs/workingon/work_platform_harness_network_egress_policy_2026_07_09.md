# work_platform_harness_network_egress_policy_2026_07_09

## Goal

推进 `v0.2.16_platform_harness_network_egress_policy`：为 WorkflowRuntime HTTP block 增加 Platform Harness network egress hard boundary。

## Scope

包含：

- 平台级 egress policy 配置：`full`、`none`、`allowlist`。
- allowlist hostname 校验。
- WorkflowRuntime HTTP block 出站请求前策略检查。
- 回归测试：`none` policy 阻断 HTTP block。

不包含：

- WebSearch / MCP / sandbox 网络工具统一收口。
- DNS/IP 级完整 egress 防火墙。
- 前端 UI。

## Linked Current Design

- `docs/current-design/design_platform_harness_network_egress_policy_v1.md`

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Audit v0.2.15 design archive gate | completed |
| 2 | Add PlatformHarness egress policy config and checker | completed |
| 3 | Wire WorkflowRuntime HTTP block policy check | completed |
| 4 | Add regression test | completed |
| 5 | Run focused and full verification | completed |
| 6 | Archive v0.2.16 with design recycling | completed |

## Acceptance Criteria

- Default policy remains `full`.
- `none` policy blocks HTTP block before network request.
- `allowlist` policy allows exact host or subdomain match.
- Secret policy remains independent.
- Full backend tests pass.

## Current Decision

Implement HTTP block egress first. Other network-capable tools will be handled as separate Platform Harness stages.

## Implementation Evidence

- Config added:
  - `platform_harness_network_egress_policy`
  - `platform_harness_network_egress_allowlist`
- Platform Harness added:
  - `enforce_network_egress_policy()`
- WorkflowRuntime:
  - HTTP block checks egress policy before resolving headers/query/body and before `httpx` request.
- Regression test:
  - `test_platform_harness_network_egress_policy_blocks_http_requests`

Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_network_egress_policy_blocks_http_requests tests/test_workflow.py::test_platform_harness_secret_policy_blocks_http_secret_headers -q
```

Result:

- `2 passed, 1 warning`

Full verification:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/platform_harness.py platform/backend/src/agent_platform/workflow_runtime.py tests/test_workflow.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `63 passed, 1 warning`

Paid/live model test:

- Not required. This stage is deterministic egress policy enforcement.
