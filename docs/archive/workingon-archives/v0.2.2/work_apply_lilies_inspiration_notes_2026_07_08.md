# work_apply_lilies_inspiration_notes_2026_07_08

## 1. Goal

把后端设计阅读笔记转化为可执行的 Lilies 开发工作：

- 区分灵感、实验、记录、不明白的问题和设计构想。
- 把灵感展开为可实现方案。
- 把设计构想展开为 `current-design`。
- 按设计逐个落代码，并把中间证据放在 `workingon`。
- 完成后等待用户检查，不自动归档。

## 2. Scope

本轮进入实现的第一优先级是“可读测试框架与反馈修复入口”，因为它直接影响 BlockFlow 的验收、人工审阅和后续自动修复。

本轮暂不归档，不创建 stage report，不筛选新的 intellectual asset。

## 3. Note Classification

| 原始笔记 | 分类 | 处理方式 |
| --- | --- | --- |
| 复杂问题前先 plan，plan 像工作流 | 灵感 + 设计构想 | 进入 `design_plan_first_modular_blockflow_builder.md`。 |
| plan 映射成积木工作流或 Builder Team 先 plan 再做 | 设计构想 | 设计 BuildPlan 到 WorkflowSpec 的转换链。 |
| 测试不过或测试结果 JSON 看不懂 | 设计构想 + 实现任务 | 进入第一优先级 `design_readable_test_frames_and_feedback_repair.md`。 |
| 测试用例按框架输出，如大纲与设定遵循度 | 设计构想 | 增加 `TestFrame`，让测试属于可读测试框架。 |
| 元测试根据结果反向修改工作流 | 灵感 + 实验 | 先实现反馈提示字段，再进入局部修复实验。 |
| 怎么确定草稿有修改 | 不明白的问题 | 记录到 question log：revision/content_hash/tested_hash。 |
| 一次添加一个节点是否比 plan 更好 | 不明白的问题 + 实验 | 进入 E01 plan-first 对比实验。 |
| 生成模板做成 RAG，自动智能体创建 loop | 灵感 + 设计构想 | 进入 `design_template_rag_reuse_marketplace.md`。 |
| 工作流复用 skill，模块化搭建再组装 | 灵感 + 设计构想 | 进入 modular BlockFlow / Template 设计。 |
| 语言影响模型理解，小模型先翻译 | 实验 | 进入 E06。 |
| claude_structure_mapping 听起来怪 | 记录 + 问题 | 记录为术语调整：这是已知 agent loop 映射，不是积木来源。 |
| agent architecture bricks 前必须 manual_search/manual_get | 不明白的问题 | 记录到 question log：代码已有硬约束。 |
| loop 怎么做到的 | 不明白的问题 | 记录到 question log：LoopConfig / runtime break condition。 |
| 复用深度设置 | 设计构想 | 进入 Template RAG 复用深度设计。 |
| required_node_types / required_tool_nodes 防黑箱 Agent | 设计构想 + 已有机制 | 当前已有，第一设计会增强可读报告展示。 |
| 是否删除 agent 节点 | 设计问题 | 暂不删除，先通过测试门禁和模板展开压低默认使用。 |
| 搭积木是找到解决方案集合 | 灵感 | 进入 PlanSpec 候选解空间和模板检索设计。 |
| Harness 如何在工作流中体现 | 设计构想 + 实验 | 进入 `design_harness_sidecar_passmode_task_monitor.md` 和 E08。 |
| 工具监管是否需要 plan first / passmode | 设计构想 | 进入 Harness sidecar / passmode。 |
| 统一 Builder/Factory 的区别 | 不明白的问题 + 设计问题 | 记录到 question log，长期方向是 AgentSpec 可展开为 WorkflowSpec。 |
| 画布自然语言修改工作流 | 灵感 + 设计构想 | 进入 assistant + NL editing 设计。 |
| 建立语言系统 | 已完成记录 | 已沉淀为 `asset_blockflow_language_system.md`。 |
| 核心技术实现如何体现，而不是平台 + 插件 | 战略灵感 | 进入助手体验、Builder-as-workflow、模板复用设计。 |
| 基本完整项目 + 实验验证留经验 | 流程记录 | 符合 evolution-development skill。 |
| 莉莉丝监视活动轨迹，多天记忆 | 产品灵感 + 风险问题 | 进入 memory surface 设计，必须有权限和数据边界。 |
| 文件系统层面封装 Codex | 产品灵感 + 风险问题 | 进入 assistant memory / FS boundary 设计。 |
| 模板市场展示 | 产品灵感 | 进入 Template marketplace 设计。 |
| 按查询难度设置 operator/workflow depth/model | 设计构想 + 实验 | 进入 complexity router 设计和 E07。 |
| Builder Team 替换成相同功能工作流 | 长期架构构想 | 进入 Builder-as-workflow 路线。 |

## 4. Current Design Plan

| 顺序 | Design | 状态 | 说明 |
| --- | --- | --- | --- |
| 1 | `docs/historical-designs/v0.2.2_design_readable_test_frames_and_feedback_repair.md` | implemented | 新增 TestFrame/readable_report/summary。 |
| 2 | `docs/historical-designs/v0.2.2_design_plan_first_modular_blockflow_builder.md` | implemented_v1 | 新增 BuildPlan 和 build_plan tool。 |
| 3 | `docs/historical-designs/v0.2.2_design_template_rag_reuse_marketplace.md` | implemented_v1 | 新增 reuse_depth suggestions。 |
| 4 | `docs/historical-designs/v0.2.2_design_harness_sidecar_passmode_task_monitor.md` | implemented_v1 | 新增 `harness.signal` event interface。 |
| 5 | `docs/historical-designs/v0.2.2_design_lilies_assistant_memory_and_nl_editing.md` | deferred | 需要权限、记忆和文件系统边界设计。 |

## 5. Acceptance Criteria

- 笔记分类已经落入 `workingon`。
- 每个实验都有 backlog，完成实验时必须生成 `.docx` 报告。
- 至少第一份 current design 有对应代码实现和测试证据。
- 实现过程写入 `implementation_*` 文件。
- 完成后不归档，等待用户检查。

## 6. Status

2026-07-08:

- Created this work file.
- Created experiment backlog and current-design set.
- Current implementation focus: readable test frames and feedback repair.

2026-07-08 implementation update:

- Implemented designs 1-4 as focused v1 code changes.
- Deferred design 5 because it needs explicit permission, memory, UI, and filesystem boundaries.
- Evidence recorded in `implementation_apply_lilies_inspiration_notes_2026_07_08.md`.
- Do not archive yet; wait for user review.

2026-07-08 archive update:

- Archived to `docs/stage-report-archives/v0.2.x/v0.2.2_apply_lilies_inspiration_notes.md`.
- Workingon files are retained for review.
- No phase report or intellectual asset was created in this archive.
