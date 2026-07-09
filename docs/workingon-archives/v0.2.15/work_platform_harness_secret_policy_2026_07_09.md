# work_platform_harness_secret_policy_2026_07_09

## Goal

推进 `v0.2.15_platform_harness_secret_policy`：为 Platform Harness 增加 secret policy hard boundary，在工具执行和 HTTP block 外部调用前阻断显式 secret 字段。

## Scope

包含：

- PlatformHarness secret field scanner。
- 配置开关：默认启用。
- AgentRuntime tool execution secret policy check。
- WorkflowRuntime Tool block 和 HTTP block secret policy check。
- 回归测试：HTTP header `Authorization` 在请求前被阻断。

不包含：

- secret store。
- secret 引用解析。
- 密钥轮换。
- 网络 egress policy。
- 对普通字符串内容做复杂 secret pattern 检测。

## Linked Current Design

- `docs/historical-designs/v0.2.15_design_platform_harness_secret_policy_v1.md`

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Audit v0.2.14 design archive gate | completed |
| 2 | Add PlatformHarness secret policy scanner | completed |
| 3 | Wire AgentRuntime and WorkflowRuntime execution checks | completed |
| 4 | Add workflow HTTP secret header regression test | completed |
| 5 | Run focused and full verification | completed |
| 6 | Archive v0.2.15 with design recycling | completed |

## Acceptance Criteria

- Secret policy is a platform-side check, not a prompt-only rule.
- Tool input with explicit secret fields can be blocked before tool execution.
- HTTP block headers/query/body secret fields can be blocked before external request.
- Existing tests pass with default policy enabled.

## Current Decision

Implement field-name-based blocking first. This catches explicit secret metadata such as `api_key`, `token`, `password`, `Authorization`, and `Cookie`; future stages can introduce a secret store and value-pattern scanning.

## Implementation Evidence

- Config added:
  - `platform_harness_secret_policy_enabled`
- Platform Harness added:
  - `enforce_secret_policy()`
  - secret field markers
- Runtime wiring:
  - `AgentRuntime._execute_tool()`
  - `WorkflowRuntime._execute_tool()`
  - `WorkflowRuntime._http()`
- Regression test:
  - `test_platform_harness_secret_policy_blocks_http_secret_headers`

Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_secret_policy_blocks_http_secret_headers tests/test_runtime.py::test_runtime_executes_tool_loop_and_persists_events tests/test_factory.py::test_factory_generates_valid_platform_agent -q
```

Result:

- `3 passed, 1 warning`

Full verification:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/platform_harness.py platform/backend/src/agent_platform/runtime.py platform/backend/src/agent_platform/workflow_runtime.py tests/test_workflow.py tests/test_runtime.py tests/test_factory.py
.venv/bin/python -m pytest -q
```

Result:

- compile passed
- ruff passed
- `62 passed, 1 warning`

Paid/live model test:

- Not required. This stage is deterministic Platform Harness enforcement.

