# Lilies 项目演进策略规范

本文档固化 Lilies 的版本化、文档驱动开发流程。它规定不同层级的报告、计划、设计、实验记录和智力资产应该放在哪里、什么时候生成、如何归档，以及如何从当前工作沉淀为下一阶段任务和长期可复用知识。

本规范只约束项目文档和开发流程，不改变后端 API、前端路由、模型类名或运行时行为。

## 1. 核心目标

Lilies 的开发流程要从“做完一个需求后留下零散记录”转向“每个阶段都有计划、过程、证据、归档和资产沉淀”。

这个流程解决四个问题：

1. 区分已完成任务和未完成任务，避免阶段交接时靠记忆判断。
2. 把已完成任务总结成可复用的项目知识，而不是只留下代码 diff。
3. 把未完成任务整理为下一阶段可执行的 plan。
4. 把复杂实验、架构演进和深度研究提炼为少而精的智力资产。

## 2. 文档层级

Lilies 的文档分为五个层级。层级越高，越像稳定结论；层级越低，越贴近当下执行。

```text
phase-reports/          大版本报告：V1.0 -> V2.0 级别的巨大阶段复盘
  └─ stage-reports/     小版本报告：V2.1、V2.2 级别的阶段总结
      └─ workingon/     当前 stage 的任务 plan、中间实验、细节完成记录
          └─ current-design/  某个任务 plan 下的具体设计和实现报告

intellectual-assets/    跨阶段可复用的高价值智力资产
```

上面的树表示依赖关系，不表示目录嵌套。实际目录统一放在 `docs/` 下：

| 目录 | 层级 | 用途 |
| --- | --- | --- |
| `docs/phase-reports/` | 大版本 | 巨大版本更新完成后的总复盘、路线转向和下一大版本目标。 |
| `docs/stage-reports/` | 小版本 | 大版本内一个阶段完成后的阶段报告。 |
| `docs/workingon/` | 当前工作区 | 当前 stage 的任务级 plan、中间实验报告、细节完成情况和临时判断。 |
| `docs/current-design/` | 具体设计 | 服务于某个任务 plan 的详细设计和实现报告，可被审阅、实现和回溯。 |
| `docs/intellectual-assets/` | 智力资产 | 经过复杂过程才获得、跨阶段可复用的高价值结论。 |

本规范不要求立即创建这些目录；执行对应工作或归档时再创建。

## 3. 版本层级

### 3.1 Phase：大版本

`phase` 是项目的巨大阶段，对应 `V1.0`、`V2.0` 这样的版本。它代表一次方向明确的大演进，例如：

- 从“能跑的原型”到“后端维护可接手的架构”。
- 从“散乱文档”到“文档驱动开发流程”。
- 从“Builder 能搭工作流”到“Platform Harness 和能力边界可验证”。

一个 phase 完成后，必须生成 `phase-report`。它回答：

- 这个大版本的目标是什么？
- 包含哪些 stage？
- 架构、业务、语言系统或开发流程发生了什么演进？
- 哪些成果已经稳定？
- 哪些任务留给下一大版本？

### 3.2 Stage：小版本

`stage` 是大版本中的小阶段，对应 `V1.1`、`V1.2`、`V2.1` 这样的版本。它代表一组相关任务完成后的可交接节点。

一个 stage 可以包含很多中间工作。每个中间工作都应该至少有一个任务级 plan；复杂任务还要展开为具体设计文档。

一个 stage 完成后，必须生成 `stage-report`。它回答：

- 上一个阶段完成了什么？
- 本阶段做了什么？
- 哪些任务已经完成，有什么证据？
- 哪些任务未完成，为什么？
- 哪些任务进入下一 stage？
- 是否产生智力资产候选？

### 3.3 Task Plan：任务级计划

任务级 plan 是 `workingon` 的基本工作单元。只要一个中间工作需要独立判断目标、范围、实现路径或验收方式，就需要写 plan。

任务级 plan 不等同于聊天里的计划。它应该固化在 repo 中，方便后续审阅、复盘和归档。

### 3.4 Current Design：具体设计

某个任务 plan 下的具体细节，如果需要被实现者反复参照，就应该展开为 `current-design`。

设计文档可以向上引用 `intellectual-assets`，例如：

- 引用 `asset_task_monitor_boundary.md` 解释为什么调度器、Builder、测试、Agent 生成都必须纳入任务监控边界。
- 引用 `asset_harness_llm_composite.md` 解释为什么某个设计应拆成 soft harness block 和 LLM 节点。
- 引用 `asset_blockflow_language_system.md` 统一 `BlockFlow`、`WorkflowSpec`、`Template` 的用语。

## 4. 目录职责

### 4.1 `docs/workingon/`

`workingon` 是当前 stage 的工作区。它保存尚未归档的计划、实验、实现细节和阶段性证据。

可以放：

- `plan_<task-topic>.md`
- `experiment_<topic>.md`
- `result_<topic>.md`
- `implementation_<topic>.md`
- 临时但有上下文价值的分析记录

不应该放：

- 已经稳定的大阶段报告
- 已经完成归档的 stage report
- 精炼后的智力资产
- 与当前 stage 无关的长期设计基线

当用户说“归档 workingon 文件夹”时，`workingon` 是主要输入。

### 4.2 `docs/current-design/`

`current-design` 保存当前开发内容的具体设计与实现报告。它比 task plan 更细，面向审阅和实现。

可以放：

- 模块级设计
- API 或数据流设计
- `BlockFlow`、`WorkflowRuntime`、`AgentRuntime`、`Platform Harness` 等具体演进方案
- 某个实验转产品化的实现设计

不应该放：

- 普通阶段总结
- 未验证的灵感碎片
- 已经被大阶段吸收且不再需要独立维护的旧设计

过期设计不一定删除，但必须在对应 stage report 中说明状态：已实现、部分实现、废弃、被新设计替代。

### 4.3 `docs/stage-reports/`

`stage-reports` 保存小版本报告。每个 stage report 是一个阶段的归档出口。

它必须把 `workingon` 中分散的 plan、实验和结果整理为：

- 完成事项
- 证据
- 未完成事项
- 下一阶段任务
- 智力资产候选

stage report 不应该复制所有中间材料。它应该总结、筛选和链接。

### 4.4 `docs/phase-reports/`

`phase-reports` 保存大版本报告。它不是每个小阶段都写，而是在一个巨大阶段完成后写。

phase report 应该基于多个 stage report 总结：

- 大版本目标是否实现
- 阶段列表和完成情况
- 项目架构、业务逻辑、语言系统或流程的关键演进
- 当前可以交接的稳定能力
- 下一大版本应该解决什么

phase report 是团队判断“项目已经从 V1.0 进入 V2.0”的证据。

### 4.5 `docs/intellectual-assets/`

`intellectual-assets` 保存智力资产。它必须少而精。

智力资产的判断标准：

1. 结论不是轻易能重新得到的。
2. 结论来自复杂实验、长阶段开发、架构演进、多文献深度阅读或高成本调试。
3. 结论能被后续多个设计或阶段复用。
4. 结论有清楚证据链和适用边界。

可以进入智力资产：

- Platform Harness 与 task monitor boundary 的稳定原则。
- Harness+LLM 复合体对 Lilies 架构的约束。
- `BlockFlow` 语言系统对项目沟通的稳定抽象。
- 某个复杂实验得出的可复用 benchmark 结论。
- 多篇论文深读后形成的设计准则。

不应该进入智力资产：

- 普通会议纪要。
- 普通 bug 修复记录。
- 某个 stage 的完整流水账。
- 没有证据链的想法。
- 可以直接从 stage report 读到的过程性内容。

## 5. 文件命名规范

| 类型 | 命名格式 | 示例 |
| --- | --- | --- |
| Phase report | `V<major>.0_<theme>.md` | `V2.0_document_driven_development.md` |
| Stage report | `V<major>.<minor>_<stage-topic>.md` | `V2.1_platform_harness_boundary.md` |
| Task plan | `plan_<task-topic>.md` | `plan_builder_benchmark_v1.md` |
| Experiment report | `experiment_<topic>.md` | `experiment_graph_similarity_eval.md` |
| Result report | `result_<topic>.md` | `result_scheduler_token_boundary.md` |
| Implementation note | `implementation_<topic>.md` | `implementation_run_cancel_path.md` |
| Current design | `design_<component-or-flow>.md` | `design_platform_harness_budget.md` |
| Intellectual asset | `asset_<stable-topic>.md` | `asset_task_monitor_boundary.md` |

命名要求：

- 使用英文小写、数字和下划线。
- 版本号只用于 `phase-reports` 和 `stage-reports`。
- `workingon` 中的文件不带版本号，归档时由 stage report 接管版本语义。
- 智力资产文件名必须稳定，不跟随某个短期任务命名。

## 6. 归档规则

### 6.1 触发条件

当用户说“归档 workingon 文件夹”时，执行归档流程。

也可以在以下场景主动建议归档：

- 一个 stage 的主要任务已经完成。
- `workingon` 中积累了多个 plan 和实验，已经影响后续检索。
- 项目方向准备切换，需要把当前工作变成下一阶段输入。

### 6.2 输入

归档输入包括：

- `docs/workingon/` 中的 task plan、实验报告、结果报告、实现记录。
- `docs/current-design/` 中与当前 stage 相关的具体设计。
- 已完成的代码、测试、运行结果、Word 报告或其他证据。
- 相关的 `LANGUAGE_SYSTEM.md`、既有设计文档和外部研究报告。

### 6.3 处理

归档时按以下顺序处理：

1. 列出当前 stage 的所有任务级 plan。
2. 对每个任务标记状态：已完成、部分完成、未完成、废弃、转入下一 stage。
3. 收集完成证据：代码路径、测试结果、报告、截图、实验输出、运行记录。
4. 把完成内容总结为 stage report。
5. 把未完成内容整理为下一 stage 任务池。
6. 筛选智力资产候选。
7. 判断是否已完成一个 phase；如果是，再生成 phase report。

### 6.4 输出

一次归档最多产生三类稳定输出：

1. 一个新的 stage report。
2. 一个新的 phase report，如果大版本确实完成。
3. 少量 intellectual assets，如果存在高价值可复用结论。

普通中间材料不应直接复制到稳定目录。稳定目录应该保存总结、证据链接和可复用结论。

### 6.5 智力资产筛选

智力资产宁缺毋滥。筛选时使用以下问题：

- 这个结论是否需要复杂过程才能获得？
- 后续多个设计是否会引用它？
- 它是否有证据链？
- 它的适用边界是否明确？
- 如果不单独保存，是否会在未来被反复重新发现？

只有多数答案为“是”时，才进入 `docs/intellectual-assets/`。

## 7. 文档模板

### 7.1 Phase report 模板

```md
# Vx.0 <大版本主题>

## 1. 大版本目标

本 phase 要解决什么问题，为什么它构成一次巨大版本更新。

## 2. 完成的 stage

| Stage | 主题 | 状态 | 证据 |
| --- | --- | --- | --- |

## 3. 架构/业务/流程演进

- 演进 1
- 演进 2

## 4. 已完成资产

- 稳定能力
- 文档资产
- 测试或实验资产

## 5. 未完成方向

- 下一 phase 需要处理的问题

## 6. 下一大版本目标

Vx+1.0 应该完成什么。
```

### 7.2 Stage report 模板

```md
# Vx.y <阶段主题>

## 1. 阶段目标

本 stage 要完成什么，属于哪个 phase。

## 2. 完成任务

| 任务 | 状态 | 证据 | 备注 |
| --- | --- | --- | --- |

## 3. 未完成任务

| 任务 | 原因 | 下一步 |
| --- | --- | --- |

## 4. 关键证据

- 代码路径
- 测试结果
- 报告或实验输出

## 5. 下一 stage 任务池

- task 1
- task 2

## 6. 智力资产候选

| 候选 | 是否进入 intellectual-assets | 理由 |
| --- | --- | --- |
```

### 7.3 Task plan 模板

```md
# plan_<task-topic>

## 1. 目标

这个任务要完成什么。

## 2. 范围

包含什么，不包含什么。

## 3. 关键决策

- decision 1
- decision 2

## 4. 实现路径

步骤、模块、数据流或文档流。

## 5. 依赖设计

引用哪些 `current-design` 或 `intellectual-assets`。

## 6. 验收标准

如何判断完成。
```

### 7.4 Current design 模板

```md
# design_<component-or-flow>

## 1. 问题

为什么需要这个设计。

## 2. 设计目标

目标、非目标、边界。

## 3. 模块边界

涉及哪些模块，每个模块负责什么。

## 4. 数据流 / 控制流

关键流程如何走。

## 5. 实现方案

具体结构、接口、状态、错误处理。

## 6. 引用的智力资产

- `docs/intellectual-assets/asset_...md`

## 7. 风险

可能失败在哪里。

## 8. 验收标准

实现后如何验证。
```

### 7.5 Intellectual asset 模板

```md
# asset_<stable-topic>

## 1. 核心结论

一句话说明这个资产是什么。

## 2. 获得成本

说明它来自复杂实验、长阶段开发、架构演进或多文献深读。

## 3. 证据链

列出支持该结论的报告、实验、代码、测试或外部资料。

## 4. 适用边界

什么时候适用，什么时候不适用。

## 5. 复用方式

后续设计如何引用它。

## 6. 禁止滥用场景

哪些情况下不能拿这个结论硬套。
```

## 8. 当前 Lilies 的归档样例

以下样例说明如何使用本规范。2026-07-08 起，早期长报告和草稿已经迁入 `docs/source-materials/2026-07_initial_architecture_research/`，精炼结论进入 `docs/intellectual-assets/`。

### 8.1 已完成阶段成果

可以作为已完成成果进入某个 stage report：

- `docs/source-materials/2026-07_initial_architecture_research/Lilies_后端核心技术设计报告.docx`：后端核心技术设计交接报告原文。
- `docs/source-materials/2026-07_initial_architecture_research/Lilies_竞品研究论文与未来方向报告.docx`：竞品、论文和未来方向报告原文。
- `docs/source-materials/2026-07_initial_architecture_research/LANGUAGE_SYSTEM.md`：项目语言系统与术语映射规范原文。
- `docs/source-materials/2026-07_initial_architecture_research/BUSINESS_LOGIC.md`：业务对象、生命周期和验收边界说明原文。
- `docs/stage-reports/V1.1_docs_consolidation_and_asset_baseline.md`：本轮文档结构整理的 stage report。
- `docs/intellectual-assets/asset_blockflow_language_system.md`：从语言系统中提炼的稳定资产（2026-07-14 更新：新增 BlockFamily 定义）。
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`：从后端报告和 Harness 讨论中提炼的稳定资产。
- `docs/stage-reports/V1.2_evolution_flywheel_and_blockflow_self_reference.md`：进化飞轮修复、SoftBlock 重构、进化流水线 BlockFlow 自引用（2026-07-14 完成）。
- `docs/current-design/design_evolution_pipeline_blockflow.md`：进化流水线 BlockFlow 的具体设计。
- `docs/workingon/plan_evolution_flywheel_closure.md`：进化飞轮闭合任务计划。
- `docs/workingon/plan_softblock_to_family_property.md`：SoftBlock 属性化任务计划。
- `docs/workingon/plan_evolution_pipeline_as_blockflow.md`：进化流水线 BlockFlow 化任务计划。

source materials 保存原文和证据链；stage report 说明阶段完成了什么；intellectual assets 只保存后续设计会反复引用的精炼结论。

### 8.2 当前完成阶段 (V1.2)

2026-07-14 完成 V1.2：进化飞轮修复、SoftBlock 重构、进化流水线 BlockFlow 自引用。

详细内容见 `docs/stage-reports/V1.2_evolution_flywheel_and_blockflow_self_reference.md`。

### 8.3 下一阶段任务 (V1.3)

可以进入下一 stage 的任务池：

- Platform Harness 生产化：把预算、权限、沙盒、网络、审计、取消和调度纳入硬边界。
- 进化任务纳入 task monitor boundary：`_auto_extract_from_build` 从裸 asyncio 任务升级为有状态机、预算、取消的治理任务。
- Builder benchmark v1：建立可衡量 `Builder Team 创建 BlockFlow` 能力边界的测试集。
- Template quality_score v2：把测试覆盖、使用、评分、成本和 Harness 完整性纳入模板质量。
- 降级信号：连续 N 次使用模板构建失败时自动降低 confidence。
- 前端模板市场：可视化浏览/搜索/评分/展开。
- Workflow-as-tool 产品化：让已发布 Version 作为上层 `BlockFlow` 的工具稳定复用。

这些内容不应直接进入智力资产，因为它们仍是待执行任务。

### 8.4 智力资产候选处理

已进入资产：

- `BlockFlow` 语言系统（2026-07-14 更新：新增 BlockFamily 定义）。
- Platform Harness 与 task monitor boundary。
- Harness+LLM 复合体。
- Lilies 竞品定位与路线优先级。

已从候选处理完毕：

- `”家族是积木的属性”` — 已并入 `asset_blockflow_language_system.md` 的 BlockFamily 定义。
- 进化流水线 BlockFlow 设计模式 — 已进入 `docs/current-design/design_evolution_pipeline_blockflow.md`。

## 9. 运行纪律

1. 开始一个复杂任务前，先在 `workingon` 写 task plan。
2. 任务需要细节审阅时，再写 `current-design`。
3. 设计文档需要引用稳定原则时，优先引用 `intellectual-assets`，不要在每个设计里重复长篇理论。
4. 完成一个小阶段时，归档为 `stage-report`。
5. 完成一个大版本时，汇总为 `phase-report`。
6. 智力资产必须经筛选，不以数量为目标。
7. 阶段报告负责承接过程，智力资产负责承接可复用结论。

## 10. 验收标准

本规范执行成功时，应满足：

1. 任意团队成员能判断一个文档应该进入 `workingon`、`current-design`、`stage-reports`、`phase-reports` 还是 `intellectual-assets`。
2. 当用户说“归档 workingon 文件夹”时，执行者知道输入、处理顺序和输出位置。
3. 一个大版本是否完成，不靠口头感觉，而靠 `phase-report`。
4. 一个小阶段是否完成，不靠聊天记录，而靠 `stage-report`。
5. 一个具体设计为什么这么做，可以向上追溯到 task plan 和必要的 intellectual asset。
6. 智力资产数量少，但每一份都能支撑后续多个设计或阶段决策。
