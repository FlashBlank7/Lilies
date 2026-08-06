# Lilies 项目语言系统

本文档定义 Lilies 团队内部沟通使用的核心术语。它的目的不是替换代码里的类名或 API 名称，而是在项目讨论、需求拆解、架构评审、交接文档和会议纪要中提供一套稳定语言，使“我们说的词”能准确映射到“代码里真实存在的对象”和“业务上真实发生的流程”。

## 1. 使用原则

1. 代码对象保留英文锚点。讨论到真实数据结构时，直接使用 `WorkflowSpec`、`AgentSpec`、`Build`、`Run` 等名称。
2. 业务动作使用项目术语。讨论“Builder Team 根据需求搭建出一个可测试积木工作流”时，称为“Builder Team 创建 `BlockFlow`”。
3. 不用“智能体”覆盖所有东西。`AgentSpec`、`BlockFlow`、`Run`、`Template` 是不同层级的对象，不能都叫 Agent。
4. 不用“工作流”覆盖所有东西。专家做事经验、DAG 图、运行记录、模板资产是四种不同对象。
5. `Harness` 必须区分软硬边界。工作流内的 `budget_gate`、`permission_gate`、`sandbox_boundary` 是 soft harness block；平台外不可被工作流删除的预算、权限、沙盒、审计和取消机制才是 `Platform Harness`。

## 2. 术语分层

| 层 | 术语 | 用途 |
| --- | --- | --- |
| 代码对象层 | `WorkflowSpec`、`NodeSpec`、`EdgeSpec`、`BlockDefinition`、`AgentSpec`、`Application`、`Draft`、`ApplicationVersion`、`Build`、`Run`、`Template` | 指向后端模型、存储和运行时对象。 |
| 业务动作层 | `Builder Team 创建 BlockFlow`、测试门禁、发布版本、模板展开、工作流固化、`Workflow-as-tool` | 指向用户从需求到交付的业务流程。 |
| 治理层 | `Harness`、`Platform Harness`、soft harness block、`permission_gate`、`budget_gate`、`sandbox_boundary`、task monitor boundary | 指向可靠性、安全、预算、权限、恢复和审计机制。 |

## 3. 核心术语词典

### `WorkflowSpec`

- 中文解释：工作流图规格。
- 英文锚点：`WorkflowSpec`
- 代码路径：`platform/backend/src/agent_platform/workflow_models.py`
- 业务定义：可被 `WorkflowRuntime` 编译和执行的 DAG 图结构，包含 `nodes`、`edges` 和 `viewport`。节点由 `NodeSpec` 表示，边由 `EdgeSpec` 表示。
- 不是什么：不是专家脑中的做事经验；不是一次运行记录；不是模板市场里的完整模板；不是一个黑箱 Agent。
- 典型句式：这个 `BlockFlow` 的底层交付物是一个 `WorkflowSpec`，它由 12 个节点和 14 条边组成。
- 禁止混用：不要把“工作流经验”“模板”“运行实例”都叫 `WorkflowSpec`。只有后端可验证、可执行的 DAG 数据结构叫 `WorkflowSpec`。

### `BlockFlow`

- 中文解释：积木流；Builder Team 创建出的可测试积木工作流。
- 英文锚点：`BlockFlow`
- 代码路径：概念名，不是当前代码类名；落地对象是 `WorkflowSpec`，由 `WorkflowBuilder` 通过 Draft 操作创建。
- 业务定义：Builder Team 根据用户需求搭建出的、由 blocks 组成、可测试、可编辑、可发布的 `WorkflowSpec` 交付物。`BlockFlow` 是团队沟通名词，用来避免把它泛称为“智能体”。
- 不是什么：不是 `AgentSpec`；不是某次 `Run`；不是 `Template` 本身；不是任意手写 DAG，除非它已进入 Lilies 的 Draft/Test/Publish 生命周期。
- 典型句式：Builder Team 为“偶像日报”需求创建了一个 `BlockFlow`，测试通过后发布为 Version 1。
- 禁止混用：不要说“Builder Team 生成了一个智能体”来指代这个对象；应说“Builder Team 创建了一个 `BlockFlow`”。

### `NodeSpec`

- 中文解释：节点规格；一个积木实例。
- 英文锚点：`NodeSpec`
- 代码路径：`platform/backend/src/agent_platform/workflow_models.py`
- 业务定义：`WorkflowSpec` 中一个具体节点的配置，包含 `id`、`type`、`block_version`、`title`、`config`、位置、重试策略、错误策略和可选 I/O contract。
- 不是什么：不是积木类型定义；不是运行时事件；不是前端 React Flow 节点本身。
- 典型句式：这个 `NodeSpec` 的 `type` 是 `http_request`，它使用 `BlockDefinition` 中注册的端口和配置 schema。
- 禁止混用：不要把 `NodeSpec` 和 `BlockDefinition` 混为一谈；前者是图中的实例，后者是积木类型定义。

### `EdgeSpec`

- 中文解释：连线规格。
- 英文锚点：`EdgeSpec`
- 代码路径：`platform/backend/src/agent_platform/workflow_models.py`
- 业务定义：`WorkflowSpec` 中连接两个节点的边，包含 `source`、`target`、`source_port`、`target_port` 和可选 `branch`。
- 不是什么：不是数据本身；不是执行事件；不是节点顺序的唯一来源。
- 典型句式：`EdgeSpec` 从 `question_classifier.output` 连到 `if_else.input`，使分类结果进入分支判断。
- 禁止混用：不要只看画布位置判断执行顺序；执行依赖来自 `EdgeSpec` 和运行时拓扑。

### `BlockDefinition`

- 中文解释：积木定义。
- 英文锚点：`BlockDefinition`
- 代码路径：`platform/backend/src/agent_platform/workflow_models.py`、`platform/backend/src/agent_platform/blocks.py`
- 业务定义：一类积木的 schema、端口、分类、版本、手册、反模式、常见错误和前端编辑器提示。`BlockRegistry` 负责注册、查询和校验。
- 不是什么：不是某个画布上的节点；不是运行结果；不是模板。
- 典型句式：Builder Team 使用 `BlockRegistry` 查询 `BlockDefinition`，再创建对应的 `NodeSpec`。
- 禁止混用：不要说“新增了一个节点类型”但只是在图里加了一个节点实例；新增节点类型意味着新增或修改 `BlockDefinition`。

### `AgentSpec`

- 中文解释：智能体配置。
- 英文锚点：`AgentSpec`
- 代码路径：`platform/backend/src/agent_platform/models.py`
- 业务定义：由 `AgentRuntime` 执行的多轮工具调用 Agent 配置，包含 `system_prompt`、工具白名单/黑名单、Skill、MCP、provider profile、权限、网络策略、轮次和预算。
- 不是什么：不是 `BlockFlow`；不是运行中的 Session；不是模板市场里的工作流模板。
- 典型句式：`AgentFactory` 生成 `AgentSpec`，`AgentRuntime` 根据这个配置创建 Session 并执行多轮工具调用。
- 禁止混用：当讨论 Builder Team 搭建出的 DAG 时，不要称它为 `AgentSpec`。`AgentSpec` 是紧凑封装的 Agent 配置，`BlockFlow` 是可编辑的积木图。

### `Application`

- 中文解释：应用入口。
- 英文锚点：`Application`
- 代码路径：`platform/backend/src/agent_platform/workflow_storage.py` 的 `applications` 表；请求模型在 `workflow_models.py`
- 业务定义：一个产品级应用容器，拥有一个可变 Draft 和多个不可变 Version。它可以是 `workflow` 模式，也可以是 `chat` 模式。
- 不是什么：不是 Draft；不是发布版本；不是一次运行。
- 典型句式：用户创建 `Application` 后，系统会初始化 revision 0 的 Draft。
- 禁止混用：不要把 Application ID 当成 Version ID 或 Run ID。

### `Draft`

- 中文解释：草稿。
- 英文锚点：Draft
- 代码路径：`platform/backend/src/agent_platform/workflow_storage.py` 的 `application_drafts` 表；内容对象是 `ApplicationSnapshot`
- 业务定义：当前可编辑工作台状态，包含 `WorkflowSpec`、Agent 绑定、测试集、revision、content hash 和最近一次测试通过的 tested hash。
- 不是什么：不是线上运行版本；不是模板；不是一次 Build。
- 典型句式：人工编辑 Draft 后，`tested_hash` 失效，必须重新测试才能发布。
- 禁止混用：不要在外部调用时默认使用 Draft；线上调用默认应绑定不可变 Version。

### `ApplicationVersion`

- 中文解释：发布版本；不可变版本快照。
- 英文锚点：Application Version
- 代码路径：`platform/backend/src/agent_platform/workflow_storage.py` 的 `application_versions` 表
- 业务定义：Draft 通过测试门禁后发布出的不可变快照，包含当时的 `ApplicationSnapshot`、content hash 和 validation report。
- 不是什么：不是 Draft；不是模板；不是运行记录。
- 典型句式：Version 2 发布后，旧 Version 1 仍保持不变，外部调用如果绑定 Version 1 不受新草稿影响。
- 禁止混用：不要把“发布了应用”说成“修改了 Draft 生效”；发布意味着生成新的不可变 Version。

### `Build`

- 中文解释：自动搭建任务。
- 英文锚点：`Build`
- 代码路径：`platform/backend/src/agent_platform/workflow_storage.py` 的 `builds` 表；运行逻辑在 `platform/backend/src/agent_platform/builder.py`
- 业务定义：一次 Builder Team 自动搭建过程，记录需求、状态、团队状态、最大轮次、最大修复轮次、错误和最终发布结果。
- 不是什么：不是 `BlockFlow` 本身；不是一次工作流运行；不是 AgentRuntime Session。
- 典型句式：这个 `Build` 进入 `needs_attention`，说明 Builder Team 没能在给定轮次和修复预算内完成可发布 `BlockFlow`。
- 禁止混用：不要把 Build 成功等同于业务工作流运行成功；Build 是搭建过程，Run 才是执行过程。

### `Run`

- 中文解释：运行记录。
- 英文锚点：`Run`
- 代码路径：`platform/backend/src/agent_platform/workflow_storage.py` 的 `workflow_runs` 表；状态模型是 `WorkflowRunState`
- 业务定义：一次 `WorkflowRuntime` 执行 `WorkflowSpec` 的记录，包含输入、节点输出、完成节点、跳过节点、等待节点、恢复值、错误和状态。
- 不是什么：不是 Draft；不是 Build；不是模板。
- 典型句式：这个 `Run` 暂停在 `human_input` 节点，恢复时需要提交 `ResumeRunRequest`。
- 禁止混用：不要说“工作流失败了”而不说明是 Build 失败、测试失败、发布失败还是 Run 失败。

### `Template`

- 中文解释：工作流模板；可复用知识资产。
- 英文锚点：`Template`
- 代码路径：`platform/backend/src/agent_platform/template_models.py`、`platform/backend/src/agent_platform/template_store.py`
- 业务定义：由 `TemplateMeta` 和 `WorkflowSpec` 组成的可复用资产。模板可以来自专家手工创建，也可以来自成功会话/构建提取；它带有分类、标签、置信度、使用量、评分和质量分。
- 不是什么：不是普通示例 JSON；不是某次运行；不是 Agent 配置。
- 典型句式：模板展开会生成新的可编辑 Draft，用户修改后可以再次发布为新模板。
- 禁止混用：不要把“模板”理解成静态示例；在 Lilies 中模板是被验证、可搜索、可展开、可评分的工作流知识资产。

### `Harness`

- 中文解释：执行约束与可靠性骨架。
- 英文锚点：`Harness`
- 代码路径：分布在 `workflow_runtime.py`、`runtime.py`、`blocks.py`、`workflow_storage.py`、`scheduler.py`
- 业务定义：让非确定性 LLM 行为可执行、可限制、可观察、可恢复、可测试的确定性机制。包括 schema、端口、DAG、权限、预算、沙盒、取消、checkpoint、事件记录和测试门禁。
- 不是什么：不是 LLM 本身；不是“更聪明的 Prompt”；不是单个积木。
- 典型句式：`model_turn` 负责 LLM 调用，但它周围的 `tool_call_router`、`permission_gate`、`budget_gate`、`event_recorder` 构成 Harness。
- 禁止混用：不要用 Harness 泛指所有架构。Harness 特指约束、执行和可靠性机制。

### `Platform Harness`

- 中文解释：平台级硬监管层。
- 英文锚点：`Platform Harness`
- 代码路径：当前是设计目标和部分机制组合，不是单一类；相关边界在 `workflow_runtime.py`、`runtime.py`、`scheduler.py`、`permissions.py`、`sandbox.py`、`workflow_storage.py`
- 业务定义：工作流外部、不可被 `WorkflowSpec` 删除或放宽的硬约束层，用于强制执行用户/项目/run 级预算、权限、沙盒、网络、审计、取消和资源生命周期。
- 不是什么：不是工作流内的 `budget_gate`；不是 Builder prompt 里的自律规则；不是前端提示。
- 典型句式：`budget_gate` 是 soft harness block；即使用户删除它，`Platform Harness` 仍应限制 token 和外部 API 消耗。
- 禁止混用：不要把工作流内的治理积木称为不可绕过的 Platform Harness。前者可被编辑，后者不可被工作流绕过。

### soft harness block

- 中文解释：工作流内软约束积木。
- 英文锚点：soft harness block
- 代码路径：`platform/backend/src/agent_platform/blocks.py` 和 `workflow_runtime.py`
- 业务定义：放在 `WorkflowSpec` 内的治理节点，例如 `permission_gate`、`budget_gate`、`round_limit`、`sandbox_boundary`、`cancellation_point`、`checkpoint_resume`、`event_recorder`。它们让约束显性化，但可被草稿编辑删除。
- 不是什么：不是平台最终安全边界。
- 典型句式：这个 `BlockFlow` 包含 `budget_gate` 和 `event_recorder`，说明它具备软 Harness，但仍需要平台级预算兜底。
- 禁止混用：不要用 soft harness block 替代 Platform Harness。

### task monitor boundary

- 中文解释：任务监控边界。
- 英文锚点：task monitor boundary
- 代码路径：当前是治理要求，不是单一类；涉及 `builder.py`、`workflow_runtime.py`、`runtime.py`、`scheduler.py`、`workflow_storage.py`
- 业务定义：所有资源消耗型动作都必须有 owner、可见 run/build 记录、预算/重试限制和取消路径。资源包括 token、网络、Docker/sandbox、外部 API、长 CPU、定时任务和 Builder Team 构建。
- 不是什么：不是只针对 scheduler 的补丁；不是前端轮询状态。
- 典型句式：定时触发、Builder Team、测试运行、Agent 生成和 WebSearch 都必须进入 task monitor boundary。
- 禁止混用：不要把“关闭前端页面”当成完整取消；后端资源消耗也必须停止或转入可见任务记录。

## 4. 业务动作术语

| 标准说法 | 定义 | 代码锚点 | 不要说成 |
| --- | --- | --- | --- |
| Builder Team 创建 `BlockFlow` | `WorkflowBuilder` 根据需求通过受控工具修改 Draft，最终得到可测试的 `WorkflowSpec`。 | `builder.py`、`applications.py`、`workflow_storage.py` | 生成智能体；吐出 JSON |
| 测试门禁 | 当前 Draft 的 mandatory tests 真实通过，且 `tested_hash == content_hash`，才允许发布。 | `workflow_runtime.py`、`workflow_storage.py` | 看起来能跑；模型说通过 |
| 发布版本 | 将已验证 Draft 固化为不可变 `ApplicationVersion`。 | `workflow_storage.py` | Draft 生效；覆盖线上 |
| 模板展开 | 从 `Template` 复制/重命名节点和边，生成新的可编辑 `WorkflowSpec`。 | `template_store.py` | 运行模板；复制示例 |
| 工作流固化 | 将专家做事方式或成功项目路径编码为可测试、可复用的 `Template`。 | `template_models.py`、`template_store.py`、`meta_cognition.py` | 保存一个 JSON |
| `Workflow-as-tool` | 已发布 Workflow 被另一个 Workflow 或 Agent 作为 Tool 调用。 | `workflow_runtime.py`、`tools/` | 复制粘贴子流程 |

## 5. 禁止混用表

| 混乱说法 | 问题 | 规范说法 |
| --- | --- | --- |
| “这个智能体是 Builder Team 搭出来的。” | “智能体”可能指 `AgentSpec`、`BlockFlow` 或泛 AI 系统。 | “Builder Team 创建了一个 `BlockFlow`。” |
| “这个 workflow 今天跑失败了。” | 不清楚是 Build、测试、发布还是 Run 失败。 | “这个 `Run` 在 `http_request` 节点失败。”或“这个 `Build` 进入 `needs_attention`。” |
| “模板就是一个示例工作流。” | 弱化了模板的验证、复用、评分和知识资产属性。 | “`Template` 是带 `TemplateMeta` 的可复用 `WorkflowSpec` 知识资产。” |
| “`budget_gate` 是 Platform Harness。” | `budget_gate` 在工作流内，可被删除，不是硬边界。 | “`budget_gate` 是 soft harness block；平台级预算属于 `Platform Harness`。” |
| “AgentFactory 和 Builder 是两个无关功能。” | 掩盖 `AgentSpec` 可展开为 Agent 架构积木链的关系。 | “`AgentSpec` 是紧凑 Agent 配置；`BlockFlow` 是显式积木图，两者是不同粒度。” |
| “发布就是把当前草稿上线。” | 发布有测试门禁和不可变版本语义。 | “发布是把当前通过测试的 Draft 固化为 `ApplicationVersion`。” |
| “这个工作流就是专家工作流。” | 专家经验和 DAG 对象不同。 | “专家工作流被固化为 `Template`；展开后形成 `WorkflowSpec`。” |

## 6. 标准叙述模板

### 6.1 从需求到交付

规范说法：

```text
用户提交自然语言需求后，Lilies 创建一个 Application 和 revision 0 的 Draft。
Builder Team 启动一个 Build，通过 BlockRegistry 查询 BlockDefinition 和 manual，
再通过 ApplicationService 对 Draft 执行 add_node、add_edge、upsert_agent、add_test 等操作。
当 Draft 中的 WorkflowSpec 形成一个可测试的积木 DAG 时，我们称 Builder Team 创建了一个 BlockFlow。
WorkflowRuntime 运行测试门禁，测试既检查输出，也检查节点结构、工具证据和 URL 引用。
只有 mandatory tests 对当前 content_hash 通过，WorkflowStorage 才允许发布 ApplicationVersion。
发布后的 Version 可以被运行调用方创建 Run，也可以进一步作为 Workflow-as-tool 被上层 BlockFlow 调用。
如果该 BlockFlow 具有复用价值，它可以发布为 Template，进入模板市场并参与 usage、rating、quality_score 的质量飞轮。
```

旧的混乱说法：

```text
用户说需求后，AI 生成一个智能体，这个智能体里面有很多工作流，
跑一跑如果能用就发布，后面也可以当模板继续用。
```

新的规范说法：

```text
用户提交需求后，Builder Team 创建一个 BlockFlow。
BlockFlow 的底层对象是 Draft 中的 WorkflowSpec。
它通过测试门禁后发布为 ApplicationVersion。
该 Version 可创建 Run，也可作为 Workflow-as-tool 被其他 BlockFlow 调用。
当它被沉淀为 Template 后，才成为模板市场中的可复用知识资产。
```

### 6.2 核心技术栈描述

规范说法：

```text
Next.js Studio 是用户编辑和观察 BlockFlow 的前端工作台。
FastAPI 的 api.py 组装后端服务，并暴露 blocks、templates、applications、builds、runs、schedules 等 API。
ApplicationService 负责把 DraftOperation 应用到 ApplicationSnapshot，维护节点、边、测试和 Agent 绑定的一致性。
WorkflowBuilder 是 Builder Team 的执行器，它使用模型和受控工具逐步修改 Draft，直到 BlockFlow 通过测试或进入 needs_attention。
BlockRegistry 持有 BlockDefinition、schema、端口、manual、模板和图校验能力。
WorkflowRuntime 编译并执行 WorkflowSpec，处理 DAG、分支、循环、human_input、agent architecture blocks、测试套件和 Run 事件。
AgentRuntime 执行 AgentSpec 的多轮工具调用循环，处理模型流、工具执行、子 Agent、权限、上下文压缩和预算。
WorkflowStorage 持久化 Application、Draft、ApplicationVersion、Build、Run 和 schedule fire，并提供 revision、idempotency、content_hash、tested_hash 等一致性边界。
TemplateStore 管理 Template 的加载、搜索、展开、注册和质量元数据。
WorkflowScheduler 发现已发布 Version 中的 schedule_trigger，并在 task monitor boundary 下创建受控 Run。
```

旧的混乱说法：

```text
前端连后端，后端让智能体生成工作流，然后 runtime 跑智能体，
模板和 schedule 也在里面，失败了再修。
```

新的规范说法：

```text
Studio 通过 FastAPI 操作 Application Draft。
WorkflowBuilder 在一个 Build 中创建 BlockFlow。
BlockRegistry 约束可用积木，ApplicationService 写入 Draft。
WorkflowRuntime 执行 WorkflowSpec 并产生 Run。
AgentRuntime 只负责执行被节点绑定或调用的 AgentSpec。
WorkflowStorage 持久化 Draft、Version、Build、Run 和测试门禁状态。
TemplateStore 将已验证 WorkflowSpec 作为 Template 复用。
Scheduler 只负责在受控任务边界内触发已发布 Version 的 Run。
```

## 7. 代码模块映射速查

| 模块 | 标准称呼 | 职责 |
| --- | --- | --- |
| `api.py` | API 组装层 | 创建服务对象，暴露 blocks、templates、applications、builds、runs、schedules 等接口。 |
| `applications.py` | Draft 操作层 | 将 `DraftOperation` 应用于 `ApplicationSnapshot`，并做草稿级校验。 |
| `builder.py` | Builder Team 执行器 | 在 `Build` 内通过受控工具创建、测试、修复、发布 `BlockFlow`。 |
| `blocks.py` | 积木目录与图校验 | 注册 `BlockDefinition`，提供 manual、template、schema、端口和 `WorkflowSpec` 校验。 |
| `workflow_runtime.py` | BlockFlow 运行时 | 执行 `WorkflowSpec`、测试套件、agent architecture blocks、pause/resume/cancel。 |
| `runtime.py` | Agent 运行时 | 执行 `AgentSpec` 的多轮模型/工具循环、子 Agent、权限、预算和上下文压缩。 |
| `workflow_storage.py` | 生命周期存储 | 持久化 Application、Draft、Version、Build、Run、schedule fire 和一致性边界。 |
| `template_store.py` | 模板市场存储 | 加载、搜索、展开、注册 `Template`。 |
| `template_models.py` | 模板模型 | 定义 `TemplateMeta`、`Template`、quality_score 和 provenance。 |
| `scheduler.py` | 定时触发器 | 根据已发布 Version 中的 schedule_trigger 创建受控 Run。 |

## 8. 语言系统验收标准

这套语言系统在团队沟通中成功，当且仅当下面两件事成立：

1. 一个新后端维护人员看到“Builder Team 创建 `BlockFlow` 后进入测试门禁，发布为 `ApplicationVersion`，再沉淀为 `Template`”时，能准确定位到 `builder.py`、`workflow_runtime.py`、`workflow_storage.py` 和 `template_store.py`。
2. 一个产品或研究讨论者说“这里需要 Harness”时，团队会追问它是 `soft harness block` 还是 `Platform Harness`，而不是默认认为加一个 `budget_gate` 就解决了安全和费用边界。

如果一句话无法判断它指向 `WorkflowSpec`、`AgentSpec`、`Build`、`Run`、`Template` 还是 `Platform Harness`，就说明这句话不符合本语言系统，需要重写。
