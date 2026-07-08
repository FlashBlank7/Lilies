# question_log_lilies_backend_design_2026_07_08

## 1. 怎么确定草稿有修改？

靠 `revision`、`content_hash` 和 `tested_hash`。

代码锚点：

- `platform/backend/src/agent_platform/workflow_storage.py`
- `ApplicationSnapshot.content_hash()` in `workflow_models.py`

机制：

1. Draft 内容保存时重新计算 `content_hash`。
2. `save_draft()` 会把 `tested_hash` 清空。
3. 测试通过后，`mark_tested(application_id, revision, content_hash, report)` 把当前 hash 记录为 `tested_hash`。
4. 发布时必须满足 `tested_hash == content_hash`。

因此“草稿有修改”的工程定义是：当前 `content_hash` 和最近一次测试通过的 `tested_hash` 不一致，或 revision 已变化。

## 2. 一次添加一个节点真的比 plan 更好吗？

不是“更好”，而是当前 Builder 的安全约束。它减少一次性吐大 JSON 的失败面，让每次 draft mutation 可追踪、可验证、可回滚。

问题在于：对复杂工作流，单节点增量可能让 Builder 过早局部最优，难以保持全局结构。

建议方向：

- 保留单步 mutation 作为提交层。
- 在单步 mutation 之前增加 `BuildPlanSpec` 和 `ModulePlan`。
- Builder 可以先生成 plan，再按模块逐步提交节点。
- 实验比较 direct incremental 和 plan-first incremental 的质量、成本和复杂度上限。

## 3. `claude_structure_mapping` 为什么听起来怪？

它的合理解释不是“先有 Claude loop，再有积木”。在 Lilies 的语言系统里，积木是显式表达能力的基础；`claude_structure_mapping` 只是把一个已有的 Claude-like agent loop 反解成显式 agent architecture blocks。

更准确的说法：

> `claude_structure_mapping` 是兼容与迁移说明，用于把已有 Claude-like loop 映射到 Lilies 的显式积木图，不代表 Lilies 的架构从单个 Agent 节点派生。

后续命名可以改为：

- `agent_loop_architecture_mapping`
- `explicit_loop_mapping`
- `legacy_claude_loop_mapping`

## 4. 使用 agent architecture bricks 前必须 `manual_search` 或 `manual_get` 是什么意思？

这是 Builder prompt 中的约束：使用 agent architecture bricks 之前，Builder 必须查阅对应积木 manual，理解配置、端口、反模式和常见错误。

原因：

- Agent architecture bricks 比业务节点更容易被误连。
- 错配 context、model_turn、tool_router、tool_executor、budget、checkpoint 会造成隐藏错误。
- manual lookup 让 Builder 在搭图前读取结构化说明，而不是只凭节点名猜。

当前它主要是 prompt 约束；后续可以升级为代码审计：如果 build 使用 agent architecture block 但没有相关 manual event，则 draft_validate 给 warning 或 error。

## 5. `loop` 怎么做到？

`loop` 是显式积木，不是隐式图环。

代码锚点：

- `LoopConfig` in `platform/backend/src/agent_platform/blocks.py`
- `_execute_loop` 相关逻辑 in `workflow_runtime.py`

机制：

- 主 `WorkflowSpec` 仍然是 DAG。
- 循环被封装成一个 `loop` 节点。
- `loop` 节点内部保存 nested `WorkflowSpec`。
- `LoopConfig.max_iterations` 限制最大循环次数。
- `break_condition` 判断是否结束。
- `output_node_id` 指定循环输出来自哪个内部节点。

这让循环显式、可配置、可停止，避免工作流图出现不可控环。

## 6. `AgentFactory` 和 `WorkflowBuilder` 有什么区别？

`AgentFactory` 生成 `AgentSpec`，`WorkflowBuilder` 创建 `BlockFlow`。

| 对象 | 产物 | 运行时 | 适用场景 |
| --- | --- | --- | --- |
| `AgentFactory` | `AgentSpec` | `AgentRuntime` | 紧凑的多轮工具调用 Agent 配置。 |
| `WorkflowBuilder` | `WorkflowSpec` / `BlockFlow` | `WorkflowRuntime` | 显式 DAG、可编辑、可测试、可发布的积木工作流。 |

统一方向不是把两者混成一个类，而是提供一个上层创建入口：

- 简单需求可生成 `AgentSpec`。
- 需要可审计和可复用时生成 `BlockFlow`。
- `AgentSpec` 可以被展开成显式 agent architecture blocks。

## 7. 要不要直接删除 `claude_agent` 节点？

不建议立刻删除。当前代码已经把它定义成 legacy compatibility wrapper。

建议路径：

1. 新建工作流默认禁止使用 `claude_agent`。
2. 测试必须声明 `required_node_types`，防止黑箱 Agent 通过。
3. 对旧草稿保留兼容。
4. 提供 migration 工具，把 `claude_agent` 展开为 context/model/tool/permission/budget/checkpoint/event 等显式架构积木。
5. 在一个大版本后再考虑移除默认展示。

## 8. Harness 怎么在工作流中体现？

应分两层：

- 工作流内 soft harness block：`permission_gate`、`budget_gate`、`sandbox_boundary`、`event_recorder`、`checkpoint_resume` 等，让约束在图里可见。
- 工作流外 Platform Harness：不可被草稿删除的硬边界，负责预算、权限、取消、超时、审计、调度和资源生命周期。

更好的表达是 sidecar 模型：主工作流走业务线，Harness sidecar 通过事件、检查点、预算请求、工具授权和取消信号与主线通信。

## 9. 工具监管是否需要 plan-first？什么是 passmode？

工具监管不一定要求完整业务 plan-first，但需要 execution plan 或 policy plan。

建议定义 `passmode`：

- `dry_run`：只生成工具调用计划，不执行。
- `approval_required`：高风险工具调用前必须人工确认。
- `guarded_auto`：低风险工具自动执行，但受预算、沙盒和审计约束。
- `manual_only`：只允许人工执行。

`passmode` 应属于 Platform Harness，不应只靠工作流节点自觉执行。

## 10. 自然语言修改画布应该怎么做？

不要把“自然语言修改”实现成重新生成整个工作流。建议使用 Draft Patch：

1. 用户输入自然语言修改请求。
2. 系统读取当前 `WorkflowSpec`、测试报告和选中节点上下文。
3. Builder 生成 `DraftPatchPlan`。
4. 用户预览 patch。
5. 应用 patch 后 `content_hash` 改变，`tested_hash` 失效。
6. 只运行受影响 tests 或全量 mandatory tests。

这样可以降低上下文压力，也避免一个小改动触发全图重建。
