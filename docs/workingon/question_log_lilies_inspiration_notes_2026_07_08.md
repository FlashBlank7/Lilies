# question_log_lilies_inspiration_notes_2026_07_08

## 1. 怎么确定草稿有修改？

当前代码使用三层信号：

- `revision`：每次 `save_draft()` 后递增，用于并发编辑和幂等操作。
- `content_hash`：由 `ApplicationSnapshot.content_hash()` 计算，代表当前草稿内容。
- `tested_hash`：测试通过后由 `mark_tested()` 记录。

发布门禁要求 `tested_hash == content_hash`。因此只要草稿被修改，`content_hash` 会变化，之前的测试结果不能继续证明当前草稿可发布。

## 2. 一次添加一个节点真的比 plan 更好吗？

一次一个节点的优点是可审计、可回滚、每一步都有工具结果，缺点是复杂 BlockFlow 的全局结构容易被限制。更合理的方向不是取消单步 mutation，而是在 mutation 之前增加 `BuildPlan`：

1. Builder Team 先形成可审阅的 plan。
2. plan 拆成模块和依赖。
3. 每个模块仍用受控 mutation 落到 `WorkflowSpec`。

这样保留可审计性，同时提升复杂度上限。

## 3. `manual_search` / `manual_get` 是什么？

这是 Builder Team 使用 agent architecture bricks 前必须执行的 manual lookup。代码里不是软提示，`builder.py` 会记录 manual lookup；如果直接添加 `agent_architecture` block 而没有先查 manual，会抛错。

业务含义：Builder Team 不能凭名字猜复杂积木的语义，必须先读 schema、manual、反模式和常见错误。

## 4. loop 怎么做到？

当前有两类 loop：

- `WorkflowRuntime` 的工作流 loop，由 loop block 配置和 `max_iterations` 控制，达到退出条件或上限就停止。
- `AgentRuntime` / Builder Team 的多轮 loop，由 `max_turns`、工具调用结果、停止状态、cancel 和预算边界共同控制。

长期方向是把所有会消耗资源或递归运行的 loop 纳入 task monitor boundary。

## 5. Builder 和 Factory 有什么区别？

当前语义：

- `WorkflowBuilder`：根据用户需求，通过受控工具创建、测试、发布 BlockFlow。
- `AgentFactory` / factory 相关逻辑：偏向生成或管理 `AgentSpec`。
- `WorkflowSpec`：可执行 DAG。
- `AgentSpec`：多轮工具调用 Agent 配置。

短期不建议直接删除 Factory 或 Agent 节点。更稳妥的路径是：让 `AgentSpec` 可以展开为显式 `WorkflowSpec` 子图，并让 Builder Team 默认生成可审计 BlockFlow。

## 6. `claude_structure_mapping` 应该怎么理解？

它不应被理解为“先有 Claude Code，所以积木来自 Claude”。更准确的表达是：Lilies 用显式积木去复现一种已知的 agent loop 结构，mapping 只是帮助维护人员理解某些 agent architecture bricks 对应的运行职责。

## 7. passmode 放在哪里？

passmode 不应该只靠 prompt。它属于 Harness / Platform Harness 设计：在某些工具监管场景下，系统可以允许低风险步骤自动通过，但必须保留审计、预算、取消和升级人工审核的边界。

