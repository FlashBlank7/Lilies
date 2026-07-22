# asset_blockflow_language_system

## 1. 核心结论

Lilies 的团队沟通必须把“用户需求被 Builder Team 搭成的可测试积木工作流”称为 `BlockFlow`。`BlockFlow` 不是新的代码模型，而是面向沟通的业务名词；它的底层代码对象是 `WorkflowSpec`。

稳定定义：

- `WorkflowSpec`：可被 `WorkflowRuntime` 编译和执行的 DAG 图结构，由 `NodeSpec` 和 `EdgeSpec` 组成。
- `BlockFlow`：Builder Team 根据需求创建出的、由 blocks 组成、可测试、可编辑、可发布的 `WorkflowSpec` 交付物。
- `AgentSpec`：`AgentRuntime` 执行的多轮工具调用 Agent 配置，不等同于 `BlockFlow`。
- `Template`：经过验证并可复用的 `WorkflowSpec` 知识资产，不只是示例 JSON。
- `Harness`：确定性执行、约束、观测、恢复和验证机制的总称；必须区分工作流内 soft harness block 与平台外 Platform Harness。
- `BlockFamily`：Agent 架构积木的语义分组，共六个家族（context / model / tool / governance / agent / skill）。**家族是积木的属性，不是积木本身。** 积木注册表保持正交（每个积木对应唯一的运行时机制），家族通过 `BlockDefinition.editor.family` 作为元数据暴露，供搜索、过滤和相似度计算使用。详见 `docs/stage-reports/V1.2_evolution_flywheel_and_blockflow_self_reference.md`。

最重要的语言规则是：不要再用“智能体”同时指 `AgentSpec`、`BlockFlow` 和泛 AI 系统。讨论代码时使用代码对象名，讨论业务交付时使用 `BlockFlow`。

## 2. 获得成本

这个资产来自多轮概念消歧、后端核心报告、会议讨论整理和语言系统文档沉淀。它解决的不是单个命名问题，而是 Lilies 项目中“Claude Code、Dify、workflow、agent、harness”等来源混在一起后导致的沟通漂移。

## 3. 证据链

- `docs/source-materials/2026-07_initial_architecture_research/LANGUAGE_SYSTEM.md`
- `docs/source-materials/2026-07_initial_architecture_research/CONCEPT_DISAMBIGUATION.md`
- `docs/source-materials/2026-07_initial_architecture_research/SEMANTIC_ANALYSIS.md`
- `docs/source-materials/2026-07_initial_architecture_research/Lilies_后端核心技术设计报告.docx`

主要代码锚点：

- `platform/backend/src/agent_platform/workflow_models.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `platform/backend/src/agent_platform/blocks.py`（`_definition()` 的 `family` 参数）
- `platform/backend/src/agent_platform/block_families.py`（`FAMILY_MAP` + `get_family()`）
- `platform/backend/src/agent_platform/merge_engine.py`（family-aware `_compute_similarity()`）
- `docs/stage-reports/V1.2_evolution_flywheel_and_blockflow_self_reference.md`
- `platform/backend/src/agent_platform/runtime.py`
- `platform/backend/src/agent_platform/builder.py`
- `platform/backend/src/agent_platform/template_store.py`

## 4. 适用边界

适用于：

- 后端维护交接。
- PR、设计文档和阶段报告中的术语统一。
- Builder Team、WorkflowRuntime、AgentRuntime、TemplateStore 相关架构讨论。
- 把复杂业务流程压缩成可复用名词时。

不适用于：

- 对外市场宣传中的宽泛表达。
- 还没有形成代码或业务边界的新想法。
- 把所有 AI 系统都硬套进 `BlockFlow`。

## 5. 复用方式

写设计文档时，先判断讨论对象属于哪一层：

| 想表达的对象 | 推荐用词 |
| --- | --- |
| DAG 结构本身 | `WorkflowSpec` |
| Builder Team 搭建出的交付物 | `BlockFlow` |
| 多轮工具调用 Agent 配置 | `AgentSpec` |
| 复用型工作流资产 | `Template` |
| 确定性约束与治理机制 | `Harness` / `Platform Harness` |

标准句式：

> Builder Team 根据用户需求创建一个 `BlockFlow`；该 `BlockFlow` 的底层表示是 `WorkflowSpec`，发布前必须通过测试门禁，发布后可以沉淀为 `Template` 或作为 `Workflow-as-tool` 被其他流程调用。

## 6. 禁止滥用场景

- 不要说“Agent 已经完成工作流”，应说“Builder Team 创建了一个 `BlockFlow`”。
- 不要把 `AgentSpec` 叫成 `BlockFlow`，除非它已经被显式展开成 `WorkflowSpec`。
- 不要把 `budget_gate` 这类工作流内 soft harness block 误写成不可绕过的 Platform Harness。
- 不要把 `Template` 当作普通示例；只有经过验证、有复用价值的 `WorkflowSpec` 才应被称为 Template。
