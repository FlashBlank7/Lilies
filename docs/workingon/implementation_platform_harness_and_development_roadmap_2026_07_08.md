# implementation_platform_harness_and_development_roadmap_2026_07_08

## 1. Implemented Changes

本轮把后续发展报告、v0.2.1/v0.2.2 stage report 和 intellectual assets 中的 P0/P1 功能任务落成代码：

- Platform Harness v1
  - 新增平台级 task monitor boundary。
  - 记录 workflow run、Builder build、test suite、scheduler trigger/manual trigger。
  - 记录 node/model/tool/nested workflow usage。
  - 支持任务预算超限失败。
  - 提供 Harness task 查询 API。
- Builder benchmark v1
  - 新增 deterministic structural benchmark。
  - 评估 node type、tool node、harness node、edge similarity、readable test frame coverage。
  - 提供 benchmark API。
- Natural-language draft patch preview
  - 新增非破坏性 preview endpoint。
  - 当前支持 rename node、describe node、remove disconnected node。
  - 不直接修改 draft revision/content_hash。
- Builder preflight test fallback
  - live paid acceptance 暴露真实 Builder 可能漏写 mandatory test。
  - Builder 收尾前若发现没有 mandatory test，会自动添加结构性 smoke acceptance test。
  - 该 test 只作为门禁兜底，后续仍应由 Builder 或人工补充任务特定测试。
- Auto-extract snapshot fix
  - 修复 ready/published 后模板自动提取把 `ApplicationSnapshot` 当 dict 访问的问题。

## 2. Files / Modules

- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/builder_benchmark.py`
- `platform/backend/src/agent_platform/draft_patch_preview.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/builder.py`
- `platform/backend/src/agent_platform/config.py`
- `platform/backend/src/agent_platform/scheduler.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `tests/test_workflow.py`
- `skills/lilies-evolution-development/SKILL.md`
- `skills/lilies-evolution-development/references/templates.md`

## 3. Verification

Deterministic checks:

- `.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py`
- `.venv/bin/python -m ruff check platform/backend/src/agent_platform/api.py platform/backend/src/agent_platform/builder.py platform/backend/src/agent_platform/config.py platform/backend/src/agent_platform/scheduler.py platform/backend/src/agent_platform/workflow_runtime.py platform/backend/src/agent_platform/platform_harness.py platform/backend/src/agent_platform/builder_benchmark.py platform/backend/src/agent_platform/draft_patch_preview.py tests/test_workflow.py`
- `.venv/bin/python -m pytest -q`

Final deterministic result:

- 54 passed, 1 warning.
- The warning is existing `fastapi.testclient` / Starlette deprecation noise.

Focused coverage added:

- Platform Harness task and workflow usage tracking.
- Platform Harness node budget violation.
- Builder benchmark missing harness nodes.
- Natural-language draft patch preview is non-destructive.
- Builder adds preflight smoke test when model omits mandatory tests.

## 4. Live / Paid Model Acceptance

- Required: yes.
- Reason: Builder quality, Platform Harness usage accounting, and real provider compatibility depend on model/tool behavior.
- Provider/model:
  - provider: DeepSeek
  - generator model: `deepseek-v4-pro`
  - runtime model: `deepseek-v4-flash`
- Budget boundary:
  - max Builder turns: 12
  - max repair cycles: 2
  - Platform Harness max model calls: 20
  - Platform Harness max tool calls: 80

Direct key smoke:

- Command: direct provider SSE call with `deepseek-v4-flash`.
- Result: received `content_block_start`, `content_block_delta`, `message_delta`, `message_stop`.

Builder live acceptance before preflight fix:

- Result: `needs_attention`.
- Harness result: `failed`.
- Usage: `model_call=12`, `tool_call=20`.
- Failure: generated draft had 3 nodes and 2 edges but no mandatory acceptance test.
- Decision impact: add Builder preflight smoke-test fallback.

Builder live acceptance after preflight and auto-extract fixes:

- Result: `ready`.
- Harness result: `succeeded`.
- Usage: `model_call=12`, `tool_call=21`.
- Draft summary: 3 nodes, 2 edges, 1 mandatory test.
- Auto-extract result: no crash; gate returned `insufficient_decisions (1)`, so no template was created.

## 5. Remaining Risk

- Platform Harness v1 is in-process and not durable across process restart.
- It records budgets and events but does not yet enforce account-level cost, network egress, secret policy, or durable retry queues.
- Builder benchmark v1 is deterministic structure scoring; it does not yet launch full Builder benchmark suites.
- Natural-language patch preview is intentionally narrow and deterministic; model-assisted editing still needs permission gates and preview-confirm flow.
- Auto smoke tests are a fallback, not a replacement for task-specific acceptance tests.

## 6. Next Design Decision

- Decision: proceed to user review before archive.
- Reason: three planned current designs have code changes, deterministic verification, and paid live acceptance evidence.
- Next action: wait for user inspection. Do not archive until the user explicitly requests archive.
