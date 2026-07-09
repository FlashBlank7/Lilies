# implementation_apply_lilies_inspiration_notes_2026_07_08

## 1. Implemented Designs

### 1.1 Readable Test Frames and Feedback Repair

状态：implemented

代码变更：

- `workflow_models.py`
  - 新增 `TestFrameSpec`。
  - `WorkflowTestCase` 新增 `frame` 和 `feedback_hints`。
  - 清理重复字段声明。
- `workflow_runtime.py`
  - `run_test_suite()` 单项结果新增 `frame` 和 `readable_report`。
  - suite report 新增 `summary`。
  - validation invalid 时也返回空 summary。
- `builder.py`
  - prompt 和 `test_add` tool description 要求 Builder Team 生成可读 test frame。
- `tests/test_workflow.py`
  - 新增 `test_run_suite_returns_readable_test_frame_report`。

验收结果：

- `.venv/bin/python -m pytest tests/test_workflow.py::test_run_suite_returns_readable_test_frame_report -q`
  - passed

### 1.2 Plan-first Modular BlockFlow Builder

状态：implemented as v1

代码变更：

- `workflow_models.py`
  - 新增 `BuildPlanModule`。
  - 新增 `BuildPlan`。
  - `BuildTeamState` 新增 `build_plan`。
- `builder.py`
  - 新增 `build_plan` tool。
  - prompt 要求复杂需求先设置 BuildPlan。
  - `build_plan` 支持 `set`、`get`、`update_module`。
- `tests/test_workflow.py`
  - 新增 `PlanFirstBuilderProvider`。
  - 新增 `test_builder_persists_plan_first_build_plan`。

验收结果：

- `.venv/bin/python -m pytest tests/test_workflow.py::test_builder_persists_plan_first_build_plan -q`
  - passed

### 1.3 Template RAG / Reuse Depth / Marketplace

状态：implemented as v1, no embedding RAG yet

代码变更：

- `builder.py`
  - `template_suggestions` 支持 `reuse_depth`。
  - `reuse_depth=none` 返回 build-from-scratch。
  - `reuse_depth=shallow` 推荐 `expand_template`。
  - `reuse_depth=deep` 推荐 `compose_modules`。
- `api.py`
  - `/api/v1/templates/suggestions` 支持 `reuse_depth`。
  - 保持 list 返回兼容性，在每个 suggestion item 上增加 `reuse_depth` 和 `recommended_action`。
- `tests/test_workflow.py`
  - 新增 `test_template_suggestions_include_reuse_depth_actions`。

验收结果：

- `.venv/bin/python -m pytest tests/test_workflow.py::test_template_suggestions_include_reuse_depth_actions -q`
  - passed

兼容说明：

- `/api/v1/templates/suggestions` 继续返回 list，避免破坏既有前端调用。

### 1.4 Harness Sidecar / Passmode / Task Monitor Boundary

状态：implemented as event interface v1

代码变更：

- `workflow_runtime.py`
  - agent architecture block executor 新增统一 `harness.signal` 事件。
  - `permission_gate` 发出 permission allowed/waiting signal。
  - `sandbox_boundary` 发出 sandbox declared signal。
  - `budget_gate` 发出 budget allowed/blocked signal。
  - `round_limit` 发出 round_limit allowed/blocked signal。
  - `hook_point` 发出 hook triggered signal。
  - `event_recorder` 发出 event recorded signal。
  - `checkpoint_resume` 发出 checkpoint saved signal。
  - `cancellation_point` 发出 cancellation clear/cancelled signal。
- `tests/test_workflow.py`
  - 扩展 `test_claude_architecture_blocks_fix_python_test_failure_without_legacy_agent`，验证运行事件中包含 `harness.signal`。

验收结果：

- `.venv/bin/python -m pytest tests/test_workflow.py::test_claude_architecture_blocks_fix_python_test_failure_without_legacy_agent -q`
  - passed

### 1.5 Lilies Assistant Memory and Natural Language Editing

状态：deferred

延期原因：

- 涉及用户活动监控、多天记忆、文件系统封装和自然语言画布修改。
- 当前缺少明确权限模型、UI 确认流程、memory store 边界和文件系统 allowlist。
- 直接实现会绕过 `asset_platform_harness_task_monitor_boundary.md` 中的 Platform Harness 原则。

下一步建议：

1. 先设计 `NaturalLanguageDraftPatch`：只生成可审阅 draft operation，不直接写入。
2. 再设计 memory surface：显式授权、可撤销、可审计。
3. 文件系统封装必须进入 task monitor boundary。

## 2. Verification

综合命令：

- `.venv/bin/python -m pytest tests/test_workflow.py::test_run_suite_returns_readable_test_frame_report tests/test_workflow.py::test_builder_persists_plan_first_build_plan tests/test_workflow.py::test_template_suggestions_include_reuse_depth_actions tests/test_workflow.py::test_claude_architecture_blocks_fix_python_test_failure_without_legacy_agent tests/test_workflow.py::test_builder_uses_incremental_brick_operations_and_publishes tests/test_workflow.py::test_builder_must_read_manual_before_agent_architecture_blocks -q`
  - 6 passed, 1 warning
- `.venv/bin/python -m py_compile platform/backend/src/agent_platform/workflow_models.py platform/backend/src/agent_platform/workflow_runtime.py platform/backend/src/agent_platform/builder.py platform/backend/src/agent_platform/api.py`
  - passed
- `.venv/bin/ruff check platform/backend/src/agent_platform/workflow_models.py platform/backend/src/agent_platform/workflow_runtime.py platform/backend/src/agent_platform/builder.py platform/backend/src/agent_platform/api.py tests/test_workflow.py`
  - passed

环境说明：

- `pytest` 不在 shell PATH 中。
- `.venv/bin/pytest` shebang 指向旧路径 `/Users/zhonghaoyang/Code/agent/claude-code-main/.venv/bin/python`，不可直接使用。
- 可用方式是 `.venv/bin/python -m pytest ...`。

## 3. Experiment Report Status

本轮没有完成正式对比实验，因此没有生成 `.docx` 实验报告。

已建立实验报告规则和 backlog。后续 E01-E10 任一实验完成后，必须在 `docs/experiment-status/reports/` 生成 `YYYY-MM-DD_HHMM_<topic>.docx`，raw evidence 进入 `docs/experiment-status/evidence/`。

## 4. Decision

原始 decision：stop for user review, do not archive.

理由：

- 前四个设计已有 v1 代码和验证证据。
- 第五个设计因为权限和隐私边界不足，已明确延期。
- 根据项目 skill，完成当前工作后等待用户检查 `workingon` 和代码结果，不能自动归档。

2026-07-08 archive update:

- User requested archive.
- Archived to `docs/stage-reports/v0.2.2_apply_lilies_inspiration_notes.md`.
- Workingon files are retained.
