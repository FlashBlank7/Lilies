# plan_apply_lilies_design_notes_2026_07_08

## 1. 目标

把 2026-07-08 的 Lilies 后端设计笔记转化为可执行的下一阶段研发输入：

- 区分灵感、实验、记录、不明白的问题、设计构想和已落地事项。
- 把高价值灵感展开成可实现方案。
- 把设计构想拆到 `current-design`，方便审阅和后续实现。
- 把需要验证的想法进入实验 backlog，并规定每个实验完成后必须生成 `.docx` 报告。

## 2. 范围

本轮只整理笔记和设计，不直接改后端代码、不改 API、不运行实验。

引用资产：

- `docs/intellectual-assets/asset_blockflow_language_system.md`
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`

输出设计：

- `docs/current-design/design_plan_first_and_modular_builder.md`
- `docs/current-design/design_structured_tests_and_feedback_repair.md`
- `docs/current-design/design_template_rag_workflow_reuse.md`
- `docs/current-design/design_harness_sidecar_and_task_monitor.md`
- `docs/current-design/design_lilies_assistant_memory_surface.md`

## 3. 分类规则

| 类型 | 判断标准 | 后续去向 |
| --- | --- | --- |
| 灵感 | 能形成新能力，但还没有明确代码边界。 | 展开成设计或实验。 |
| 设计构想 | 已经有目标功能和大致机制。 | 进入 `current-design`。 |
| 实验 | 需要用对照、指标或样例验证，不应直接产品化。 | 进入实验 backlog，完成后生成 `.docx` 报告。 |
| 问题 | 对现有代码或概念不清楚。 | 先写清楚答案和代码锚点。 |
| 记录 | 对现状、方向或策略的观察。 | 进入 stage report 或作为设计背景。 |
| 已落地/已有资产 | 已经有文档或代码机制支撑。 | 引用现有资产，不重复建设。 |

## 4. 笔记分类表

| 原始笔记主题 | 分类 | 判断 | 处理结果 |
| --- | --- | --- | --- |
| 思考任务也先 plan，plan 像工作流 | 灵感 + 实验 | 可能成为 Builder Team 的前置规划层。 | 进入 `design_plan_first_and_modular_builder.md`，实验 E01。 |
| 让智能体团队先 plan 再做工作流 | 设计构想 | 与 Builder pipeline 直接相关。 | 进入 plan-first Builder 设计。 |
| AI 生成测试用例输出难读 | 设计构想 | 现有 `WorkflowTestCase` 偏结构，缺少可读测试框架。 | 进入结构化测试设计。 |
| 元测试 + 根据测试结果微调节点 | 设计构想 + 实验 | 需要验证微调优于整体重建。 | 进入结构化测试设计，实验 E04。 |
| 怎么确定草稿有修改 | 问题 | 已有 `content_hash/tested_hash/revision` 机制。 | 进入问题日志。 |
| 一次添加一个节点是否限制复杂度 | 问题 + 实验 | 当前 Builder prompt 强制单步 mutation，可能限制复杂图。 | 进入 plan-first 设计，实验 E02。 |
| 生成模板做 RAG，形成自动智能体创建 loop | 灵感 + 设计构想 | 与 TemplateStore 和 Builder loop 相关。 | 进入 template RAG 设计，实验 E05。 |
| 工作流复用：模块化搭建、封装、组装 | 设计构想 | 是提升复杂度的核心路径。 | 进入 modular Builder 和 template reuse 设计。 |
| 语言影响模型理解，小模型先翻译再用 | 实验 | 需要模型对照和任务对照。 | 进入实验 E07。 |
| claude_structure_mapping 听起来怪 | 问题 | 它是现有 Claude-like loop 到显式积木的映射，不代表先后本体论。 | 进入问题日志。 |
| 使用 agent architecture bricks 前必须 manual_search/manual_get | 问题 | Builder prompt 要求查 manual 后再配置不熟悉的架构积木。 | 进入问题日志。 |
| loop 怎么做到 | 问题 | `loop` 是显式 block，内含 nested WorkflowSpec 和 `max_iterations`。 | 进入问题日志。 |
| 复用深度设置 | 设计构想 | 应成为模块复用和模板展开的治理参数。 | 进入 template RAG 设计。 |
| 测试必须包含 required_node_types / required_tool_nodes | 已落地 + 设计延伸 | 代码已有字段和校验，可继续发展成测试框架。 | 进入结构化测试设计。 |
| 要不直接删除 agent 节点 | 设计构想 | 当前 `claude_agent` 是 legacy wrapper，不应默认用于新图。 | 进入结构化测试和 migration 策略。 |
| 搭积木是找到解决方案集合的行为 | 记录 + 灵感 | 可作为 Builder benchmark 的评价视角。 | 进入实验 E01/E02 背景。 |
| Harness 怎么在工作流中体现 | 设计构想 + 问题 | 需要区分 sidecar Harness 和 soft harness block。 | 进入 harness sidecar 设计。 |
| 工具监管不需要 plan first？应该有 passmode | 设计构想 | 工具监管应进入 Platform Harness，passmode 是执行策略。 | 进入 harness sidecar 设计。 |
| 统一 Builder/Factory，AgentSpec 与 WorkflowSpec 区别 | 问题 + 设计构想 | 需要统一入口，但不混淆两种对象。 | 进入问题日志和 plan-first 设计。 |
| 工作画布上用自然语言修改工作流 | 设计构想 | 应是 Draft patch layer，而不是重新生成全图。 | 进入结构化测试和 modular Builder 设计。 |
| 建立语言系统 | 已落地 | 已有 `asset_blockflow_language_system.md`。 | 引用资产，不重复建设。 |
| 平台核心能力如何融合小模型 | 灵感 + 实验 | 需要把小模型放在规划、翻译、分类、检索等低风险环节。 | 进入 assistant/memory 设计和实验 E07/E12。 |
| 做完整项目，把时间用于实验验证并留下经验 | 记录 | 与文档驱动开发流程一致。 | 进入本 plan 和实验报告规范。 |
| Lilies 监视活动轨迹和多天记忆 | 设计构想 | 是助手化产品面的长期方向。 | 进入 assistant memory 设计。 |
| 对 Codex 做文件系统封装变成助手 | 设计构想 | 需要权限、沙盒和可审计文件代理。 | 进入 assistant memory 设计。 |
| 模板市场展示可复用模板 | 设计构想 | 与 TemplateStore/quality_score/marketplace 相关。 | 进入 template RAG 设计。 |
| 根据查询难度设置 operator、workflow depth 和模型 | 设计构想 + 实验 | 是 routing policy 和 cost-quality tradeoff。 | 进入 assistant/memory 设计，实验 E12。 |
| Builder Team 同义替换成工作流，Lilies 升级为通用助手 | 长期设计构想 | 当前不是现实能力，但可以分阶段下沉。 | 进入 assistant/memory 设计和 plan-first 设计。 |

## 5. 下一阶段切片

### V1.2：可验证 Builder 与测试可读性

目标是解决“生成后不知道测试在测什么、失败后只能整体重来”的问题。

优先任务：

1. 结构化测试框架设计。
2. 人类反馈驱动的节点级修复设计。
3. plan-first Builder 实验。
4. 一次一个节点 vs plan/batch mutation 对照实验。

### V1.3：模块化复用与 Template RAG

目标是解决“复杂工作流难以一次搭成”的问题。

优先任务：

1. Workflow module / reusable BlockFlow module 定义。
2. Template RAG 检索、展开、适配、测试、再索引 loop。
3. 复用深度和循环预算约束。
4. 模板市场最小展示面。

### V1.4：Platform Harness 与 Sidecar Harness

目标是解决“工作流内 harness 能表达，但不能作为硬边界”的问题。

优先任务：

1. 任务监控边界统一。
2. Harness sidecar 与 workflow 主线通信设计。
3. Tool governance passmode。
4. 预算、权限、取消、审计的统一 UI/API 视图。

### V2.x：Lilies 通用助手化

目标是让 Lilies 不只是平台，而是能被用户日常调用、带长期记忆、能调取项目资产的助手。

优先任务：

1. Activity memory。
2. 文件系统代理。
3. 自然语言修改画布。
4. Builder-as-workflow。
5. 小模型在翻译、分类、检索、低风险规划中的使用。

## 6. 验收标准

- 每条原始笔记都能在分类表中找到归属。
- 每个高价值设计都有 `current-design` 文件承接。
- 每个需要验证的观点都有实验编号。
- 每个实验完成后必须生成 `.docx` 报告，命名包含完成时间和主题。
- 后续实现不能直接引用聊天记录，应引用本 plan、对应 design 或实验报告。

## 7. 执行状态

本 plan 采用文档演进式开发：先完成 plan 和 design backlog，再逐份完成 `current-design`；每完成一份设计，更新状态并单独提交。

### 7.1 计划层状态

| 产物 | 状态 | 说明 |
| --- | --- | --- |
| 笔记分类表 | 已完成 | 2026-07-08 设计笔记已全部分类并给出去向。 |
| 问题日志 | 已完成 | 已回答草稿修改、loop、AgentFactory/WorkflowBuilder、Harness、passmode、自然语言 patch 等问题。 |
| 实验 backlog | 已完成 | 已定义 E01-E13，并规定实验完成后必须产出 `.docx` 报告。 |
| current-design backlog | 已完成 | 已拆出 5 份设计草案，等待逐份扩充和完成。 |

### 7.2 Current-design 状态

| current-design | 状态 | 下一步 |
| --- | --- | --- |
| `design_plan_first_and_modular_builder.md` | 已完成 | 可进入下一阶段实现；后续需做 E01/E02/E06 实验。 |
| `design_structured_tests_and_feedback_repair.md` | 已完成 | 可进入下一阶段实现；后续需做 E03/E04/E08/E11 实验。 |
| `design_template_rag_workflow_reuse.md` | 已完成 | 可进入下一阶段实现；后续需做 E05/E06/E01 实验。 |
| `design_harness_sidecar_and_task_monitor.md` | 已完成 | 可进入下一阶段实现；后续需做 E09/E10/E12 实验。 |
| `design_lilies_assistant_memory_surface.md` | 草案 | 补齐权限边界、memory schema、隐私风险和阶段化验收。 |

### 7.3 归档规则

当前 stage 暂定为 `V1.2_design_notes_to_current_design`。只有当 5 份 `current-design` 都达到“已完成”状态，并且实验 backlog 与问题日志已能支撑下一阶段实现时，才归档新的 stage report。
