# Lilies 能力边界设想落地成果审查报告

**审查日期：** 2026-07-17

**审查对象：** `docs/lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx` 提出的产品、架构、评测、治理与场景设想

**当前代码基线：** `usabilityEnhence` 分支，HEAD `82a308e`，运行时 `v0.4.10`
**审查原则：** 只把代码、测试、可重复运行或浏览器证据支持的内容列为成果；不把设计文档、状态字段或未来环境债务当成能力完成。

## 1. 执行结论

这段时间不是只补了几处界面，而是把能力边界报告中的路线，从分析模型推进成了一套可运行的平台机制。`v0.4.2` 至 `v0.4.10` 共形成 9 个连续版本、42 个提交，变更覆盖 272 个文件，累计约 40,225 行新增、2,984 行删除。报告登记的 36 个意图目前均有实现和验证证据，其中包括 9 个产品意图、8 个架构意图、4 个评测意图、4 个治理意图、3 个压力场景和 8 个长任务演进控制意图。

但必须把这句话说完整：**36 个意图“在各自声明的证据上限内完成”，不等于 Lilies 已经生产可用。** 当前最强成果主要位于 H2 组件验证与 H3 本地/受控测试租户集成。H4 真实客户环境、H5 生产观测、长期 SLO、真实账单对账、任意网站合法访问、客户 IdP 和私有部署合规都没有完成，也没有被本报告包装成完成。

对项目现状最准确的判断是：

- Lilies 已从“能生成和画工作流的原型”发展为“具有能力建模、可编辑运行、分级评测、持久任务、连接器硬控制和全局治理的本地集成平台”。
- 它已经能够严肃支持内部验证、受控演示和测试租户试点。
- 它还不适合直接承诺给非技术客户自行完成任意智能体搭建，更不能据此承诺生产级无人值守或深度客户系统集成。
- 当前分支正在补最后一类很现实的问题：架构机制存在，但普通用户点击、等待、理解状态和完成整条路径时仍会遇到体验或状态一致性缺陷。

## 2. 审查依据与最新验证

本次审查直接核对了以下证据：

1. 能力边界报告最新版及其 F/G/X、E0-E5、H0-H5、三种产品模式和三类界面路线。
2. `docs/evolution-control/report_intents.json` 中 36 个稳定意图及其证据锚点。
3. `docs/stage-reports/v0.4.2_*` 至 `v0.4.10_*` 的阶段合同、完成项、证据债务与声明上限。
4. `docs/workingon-archives/v0.4.3/` 至 `v0.4.10/` 的浏览器、组件、回归和校验归档。
5. 当前工作区代码、未归档修复、真实平台健康状态和临时人类旅程回放。

2026-07-17 的重新验证结果：

| 验证 | 结果 | 含义 |
| --- | --- | --- |
| 完整 Python 测试 | `741 passed, 85 xfailed, 0 failed`，1 个依赖弃用 warning，95.12 秒 | 当前代码没有新增未预期测试失败；85 个 xfail 是已登记历史冲突，但仍是技术债，不等同于不存在问题。 |
| 当前修复定向测试 | `43 passed`，1 个依赖弃用 warning | 自然语言编辑、需求补全、持久任务、Connector 隔离、Builder 进度和状态边界均通过。 |
| Ruff | 通过 | 当前涉及的 Python 文件无静态检查错误。 |
| 前端 TypeScript | `tsc --noEmit` 通过 | 当前前端类型检查通过。 |
| 前端生产构建 | Next.js production build 通过 | 主页、应用详情、Customer Runtime、Governance 和 API proxy 路由均成功构建。 |
| 当前运行状态 | 后端 `/health` 为 `ok`，前端返回 HTTP 200 | 平台当前可访问；运行时正确标识为 `v0.4.10 / v0.4.x`。 |

## 3. 与能力边界报告路线逐项对照

### 3.1 P0：意图保持与长任务控制

**报告设想：** 用稳定意图、Stage Contract、唯一下一任务来源、偏移记录和独立闭环审查，防止小阶段漏做、长期迭代变味或中断后另起炉灶。

**已经完成：**

- `v0.4.2` 建立了 Program Charter、36 项机器可读意图登记、版本 Stage Contract、阶段报告 v2 模板、闭环审查与版本复杂度门槛。
- `v0.4.3` 至 `v0.4.10` 每一版都从上一版稳定任务 ID 接续，保留 mandatory 任务、证据、偏移和下一阶段授权。
- 最终阶段明确停止继续从总结中自行发明任务，并把外部环境不足保留为 evidence debt，而不是假装完成或无限阻塞内部机制建设。

**审查判断：** 机制层面完成。它成功防止了能力边界报告中的主要路线在多次版本推进中消失。不过，这只证明仓库控制协议存在，不证明任何执行者永远不会偏移；真正的保证仍来自每次对 Stage Contract、代码和运行证据的重新核对。

### 3.2 P0：Quick / Guided / Governed 三种模式

**报告设想：** 轻量工作流不应被重型验收挡住；高风险流程仍应受硬策略约束。

**已经完成：**

- `v0.4.3` 增加 Quick、Guided、Governed 交付模式及持久化策略。
- Quick/Guided 的验收改为建议性证据，用户可在看见风险提示后发布。
- Governed 模式可以绑定不可绕过的发布与运行策略。
- 行为修改会使旧验收证据 stale，并提示重验，但不会删除草稿或把所有发布入口统一锁死。

**审查判断：** 产品策略与后端行为已实现并有测试。实际界面仍需要继续降低普通用户理解成本，尤其是验收、证据等级和治理术语的呈现。

### 3.3 P0：面向人的积木配置与统一 WorkflowSpec

**报告设想：** 点击积木应看到领域配置器，原始 JSON 只作为专家视图；保存结果必须真正改变工作流运行语义。

**已经完成：**

- `v0.4.3` 为 LLM、HTTP/工具、Loop 等常用积木增加 schema 驱动配置。
- 配置写回唯一 WorkflowSpec，触发 revision 和 evidence stale 语义。
- 当前未归档修复进一步处理了点击积木崩溃、积木摘要、选中引用、一键整理画布和 WASD 画布移动。
- 最新人类维护者回放依次点击 6 个积木，均显示角色、配置概览和下一步，没有控制台错误或失败请求。

**审查判断：** 核心机制完成，近期崩溃问题已有工作区修复和人类回放证据，但尚未归档为新版本，因此不能把当前体验稳定性写成正式版本承诺。

### 3.4 P0：验收建议、修复上下文与自然语言工作流编辑

**报告设想：** 失败用例不能只显示红色；应形成结构化建议，并把 WorkflowSpec、相关节点、Trace 和失败上下文交给 Builder Team 生成可预览补丁。

**已经完成：**

- `v0.3.54`、`v0.4.3` 和 `v0.4.5` 建立了失败建议、repair preview 和能力合同上下文。
- 自然语言编辑从少数固定指令扩展为整个工作流范围的模型优先编辑；引用积木只提供上下文，不把修改范围锁死。
- 当前工作区修复了“unsupported instruction”假限制、中文复合指令误改 requirement、模型操作缺少临时幂等键和非法操作未提前校验等问题。
- 真实模型预览已能只更新指定业务节点描述；人类回放也生成了精确的节点重命名与工作流描述修改操作，没有提前修改草稿。
- “运行全部真实测试”现在会立刻显示运行中状态；一次真实回放中 4 个用例约 27 秒后全部通过，HTTP 200，无控制台或请求错误。

**审查判断：** 预览与人工确认修复机制已经存在，真实验收的运行反馈也已修好。**尚未重新完成一个“先制造失败用例 → 自动生成 repair preview → 应用 → 重跑转绿”的完整浏览器闭环**，因此不能把“自动修复失败工作流”写成当前已全面验收。

### 3.5 P0：F/G/X 能力需求模型与 Capability Build Contract

**报告设想：** 需求不能只变成一段自然语言计划；应拆成 F 功能能力、G 运行保证、X 外部契约，展开依赖、运行闭包、承载位置、证据计划和声明范围。

**已经完成：**

- `v0.4.5` 实现 typed F/G/X 合同、requires/excludes、依赖闭包、E0-E5 envelope、外部环境可用性和 carrier 决策。
- Builder Team 生成并持久化 Capability Build Contract，应用、版本、构建任务和前端均能读取。
- 报告中的三个压力需求会得到不同合同：Codex-like 偏 E2 工具反馈闭环；每日采集偏 E3 持久调度；客户嵌入偏 E4/E5 身份、租户、写回和补偿。
- 缺环境时可以输出 `blocked_by_environment` 或较低 claim ceiling，而不是通过改写验收标准声称整体可交付。

**审查判断：** 报告最核心的建模纠偏已经落成平台数据结构和 Builder 消费路径。广泛真实模型语义质量仍只做了有限验证，不能据此声称任何模糊需求都能稳定抽取出正确合同。

### 3.6 P1：结构化 Loop 与 Codex-like 工具反馈闭环

**报告设想：** 不开放任意有环图，而把 Loop/Subflow 做成具有状态、条件、上限、取消、checkpoint、逐轮 Trace 和工具结果回灌的一等能力。

**已经完成：**

- `v0.4.4` 建立 Codex-like workspace agent 压力场景。
- 确认并测试 `model → tool → result → model` 两轮反馈、真实注册的本地 Read 工具、停止/取消、权限/沙盒声明、事件和 checkpoint。
- Loop/Subflow 保持结构化语义，不通过放开任意画布环路规避终止与副作用问题。

**审查判断：** H2 组件级闭环完成。它证明 Lilies 有真正的工具反馈循环，不只是顺排积木；但没有证明不受限 shell、通用 Web/Computer Use、跨进程长任务和任意代码库任务的 Codex 等价能力。

### 3.7 P1：可复用模块与 Capability Evidence Registry

**报告设想：** 好工作流应成为带输入输出、依赖、版本、证据和边界的模块，而不是只保存提示词；平台能力声明应可下钻到证据。

**已经完成：**

- `v0.4.6` 实现不可变精确版本模块、typed ports、依赖、运行 envelope、风险、已知边界、H0-H5 claim ceiling 和内容寻址证据。
- Builder 可以选择兼容模块，Engineer Studio 可以查看并插入精确版本。
- Capability Evidence Registry 将实现、配置、API、测试、证据、遥测、缺口和声明等级连成可查询记录。
- 明确拒绝用调用次数近似值冒充 token 或账单级指标。

**审查判断：** 模块注册和证据声明机制在 H2/H3 本地范围内完成。还没有真实第三方模块生态、远程注册表、生产兼容性治理或长期版本迁移证据。

### 3.8 P1：Customer Runtime / Engineer Studio / Governance Console 三界面

**报告设想：** 客户只看目的、输入、进度、结果和可恢复错误；工程师编辑能力与证据；治理者跨应用看任务、成本、策略、队列和 Trace。

**已经完成：**

- `v0.4.7` 将三类界面拆成独立路由和信息架构。
- Customer Runtime 隐藏节点 JSON、策略内部字段、原始签名、secret 和工程映射步骤。
- Engineer Studio 保留画布、配置、构建、测试、自动化和集成管理。
- Governance Console 增加跨应用任务、父子 Trace、worker/queue、策略、预算、token/cost 支持状态、能力证据和后续 Connector/持久任务视图。
- 归档包含桌面/移动端浏览器证据和真实 Customer Runtime 启动到结果回放。

**审查判断：** 信息架构已真正拆开，不再只是改标签。治理数据主要证明本地平台机制；权威账单对账、生产告警、值班和真实多租户 SLO 仍不存在。

### 3.9 P1：H0-H5 Evaluation Harness 与能力驱动用例

**报告设想：** 自动用例应由能力集合、运行闭包、外部契约和证据目标共同生成；没有真实环境时应降级或阻塞，不能伪造通过。

**已经完成：**

- `v0.4.8` 实现 H0-H5 Profile、mock/contract/sandbox/live/production-observation 环境、兼容矩阵、可用性和 mutation boundary。
- 自动用例生成覆盖能力、依赖、环境契约、调度、重试、身份、幂等、补偿等真实 carrier。
- 结果可表达 `design_only`、`static_verified`、`component_verified`、`integration_verified`、`live_verified`、`production_observed`、`blocked_by_environment` 和 `unsupported`。
- claim 取所选 Profile、环境、用例、外部契约和实际结果中的最弱上限；H4/H5 默认不可用，不能靠选择标签升级声明。

**审查判断：** 评测编排与诚实声明机制在 H3 本地集成范围内完成。H4/H5 的真实实例证据没有完成，这正是当前边界，而不是实现失败。

### 3.10 P1：E3 持久任务与每日网页采集压力场景

**报告设想：** schedule 节点不等于长期任务；需要 durable history、原子认领、lease、retry/resume、幂等、取消、去重、来源和可观察状态。

**已经完成：**

- `v0.4.9` 建立持久任务存储、原子 claim、lease fencing、重试/恢复、取消、计划触发去重和运行历史。
- 每日采集场景包含受控 HTTP 来源、来源访问决策、canonical/content 去重、provenance、摘要和 Customer Runtime 展示。
- Engineer Studio 可以查看和恢复任务，Governance 可以查询持久任务证据。
- H1 静态和 H3 受控本地合同运行通过；H4 任意真实站点访问保持 `blocked_by_environment`。

**审查判断：** E3 的本地持久执行底座已建立。没有任意网站合法抓取保证、通用浏览器兼容、外部通知回执、分布式 exactly-once、生产 worker 可用率或长期无人值守 SLO。

### 3.11 P2：Connector / 深度客户系统嵌入与高风险治理

**报告设想：** 深度嵌入不能用一个通用 HTTP 积木冒充；需要身份、租户、schema、权限、幂等写回、回调、补偿、部署 profile、审计和紧急停止。

**已经完成：**

- `v0.4.10` 实现不可变版本 Connector manifest、请求/响应 schema、read/write/compensation 操作和 mock/test/live/private profile。
- 实现外部 subject 到 Lilies tenant/actor/role 的签名映射、过期与 nonce 防重放、secret reference 注入和跨租户拒绝。
- 平台侧策略在 adapter 前强制 operation/profile allowlist、payload 限制、角色、dry-run、精确 payload 预授权、revision、使用次数和 emergency stop。
- 写回产生持久 side-effect receipt，支持幂等、冲突拒绝、失败副作用不确定性、签名有序回调、显式补偿和演练证据。
- 场景仍是可编辑工作流，并接入 Customer Runtime、Engineer Studio、Evaluation Harness 和 Governance。
- 当前未归档修复进一步修正了 Connector 列表、策略、执行和演练跨应用串数据，以及没有配置时错误借用全局第一个租户的问题。

**审查判断：** H3 受控测试租户集成完成，是这段时间最完整的高复杂度纵向成果。没有真实客户 IdP、客户 live mutation、认证级租户隔离结论、客户 VPC/KMS、私有部署合规、生产 SLO 或 H5 事故演练。

## 4. 三个压力场景目前到底做到哪一步

| 场景 | 报告要求 | 当前最强证据 | 已经证明 | 没有证明 |
| --- | --- | --- | --- | --- |
| Codex-like 工作流 | E2 工具反馈闭环，必要时进入 E3 | H2 组件验证 | 模型能读取工具结果后继续决策；有状态、停止、预算、权限/沙盒声明、Trace 和 checkpoint | 与 Codex 等价的通用代码库能力、任意 shell/Web、跨进程长任务和广泛真实模型质量 |
| 每日网页采集 | E3 durable job，来源、去重、恢复和证据 | H3 受控本地 HTTP 集成 | 计划触发、持久认领、lease、重试/恢复、幂等、来源和客户摘要 | 任意网站授权、通用爬虫、外部通知、分布式生产可靠性和长期 SLO |
| 客户系统深度嵌入 | E4/E5 身份、租户、治理、写回、回调和补偿 | H3 受控测试租户集成 | typed Connector、签名身份、硬策略、幂等 receipt、callback、compensation、三界面和评测治理 | 真实客户 IdP/系统、live mutation、私有部署合规、生产 SLO、事故响应和 H5 观测 |

这张表也回答了集合模型与层次模型是否真正被应用：三个场景不是按“简单/中等/复杂”套不同模板，而是分别实现了不同 F/G/X 能力集合，再在 H2/H3 证据层级上验证。

## 5. 当前未归档的可用性与一致性修复

已归档能力完成后，真实模拟用户使用又发现了一批“机制存在但用户会误以为坏了”的问题。当前工作区有 20 个已跟踪文件变更，以及 3 个人类旅程脚本和 5 个新增测试文件；相关定向测试 43 个全部通过，但尚未形成新版本归档。

### 5.1 已修复并验证

- **自然语言工作流编辑：** 从固定 rename/description/remove 限制改为模型优先的全工作流补丁；引用积木只是上下文；预览不提前修改草稿；非法跨边界操作在应用前验证。
- **Builder 进度：** 每个成功工具操作后立即持久化 team state，前端可以看到任务数从 0 逐步增加，而不是结束后突然出现结果。
- **真实验收反馈：** 点击后立刻显示所有用例正在运行；一次真实回放 4/4 通过，约 27 秒完成，无控制台和请求错误。
- **定时状态：** 普通应用明确返回 `not_configured`，有未发布 schedule 返回 `draft_unpublished`，不再用 404 和字符串猜测状态。
- **Connector 隔离：** manifests、bindings、policies、executions、exercises 按 application/tenant 范围查询；普通应用不再显示别的应用租户和回执。
- **应用卡片描述：** 保留原始审计描述，同时优先显示 Capability Build Contract 的干净 business goal，避免把旧 Markdown 需求全文塞进卡片。
- **画布操作：** 6 个节点连续点击无崩溃；右键/选择引用、一键整理和 WASD 平移均有回放证据。

### 5.2 仍未闭环

- 当前新增应用的“自然语言需求 → 选项补全 → Builder 发布 → Customer Runtime”旅程已经走到新应用生成、7 个构建任务、1 次修复、6 个节点和发布，但脚本在 Customer Runtime 目的文案检测处停止。现有已发布应用的 Runtime 可单独正常显示；这条新应用全链路仍应再跑到最终业务结果后才能算完全闭环。
- 失败验收的自动 repair preview 有单元/集成机制证据，但当前修复后还缺一次完整浏览器回放：制造真实失败、看到建议、预览补丁、应用、重跑变绿。
- 当前修复还在脏工作区，未形成 stage report、归档提交和可回退版本；它们是“已验证候选成果”，不是正式发布成果。

## 6. 报告路线完成度矩阵

| 报告工作包 | 机制实现 | 本地/受控验证 | H4/H5 真实证据 | 当前判断 |
| --- | --- | --- | --- | --- |
| Product Intent & Evolution Control | 是 | 是 | 不适用 | 完成机制 |
| Quick / Guided / Governed | 是 | 是 | 未做真实组织策略 | 本地完成 |
| Human-readable Block Configuration | 是 | 是，近期崩溃已修 | 无广泛客户可用性研究 | 已实现，继续打磨 |
| Advisory Acceptance & Repair Loop | 是 | 部分完整；通过路径已回放 | 无生产修复指标 | 缺失败到修复浏览器闭环 |
| Capability Requirement Schema | 是 | 是 | 无广泛真实模型质量基准 | H2/H3 机制完成 |
| Builder Capability Build Contract | 是 | 是 | 无任意客户需求保证 | H2/H3 机制完成 |
| Template / Module Registry | 是 | 是 | 无真实生态/远程注册表 | H3 本地完成 |
| 三类前端 | 是 | 是，含桌面/移动回放 | 无大规模非技术用户研究 | H3 本地完成 |
| Governance Console | 是 | 是 | 无生产账单/SLO/值班 | H3 本地完成 |
| Structured Loop | 是 | H2 Codex-like 组件回放 | 无通用长任务生产证据 | H2 完成 |
| Evaluation Harness | 是 | H0-H3 是 | H4/H5 默认阻塞 | 机制完成，外部证据未完成 |
| Durable Job Substrate | 是 | H3 受控来源 | 无长期生产 SLO | H3 完成 |
| Connector / Embedding SDK | 是 | H3 测试租户 | 无真实客户/私有部署 | H3 完成 |
| 高风险自治治理 | 是 | H3 policy/stop/compensation 演练 | 无生产事故演练 | H3 完成 |

## 7. 仍然不能对外承诺的事项

以下内容即使在意图登记中显示 `implemented_verified`，当前也不能对投资方、客户或内部决策者表述为已完成生产能力：

1. Lilies 能像 Codex 一样稳定处理任意真实代码库任务。
2. Lilies 能合法、稳定地抓取任意主流网站并长期无人值守运行。
3. Lilies 已经与真实客户系统、真实 IdP、真实生产数据和真实回调网络完成集成。
4. Lilies 已经具备生产级多租户认证、SLO、值班、告警、计费对账和事故响应。
5. 选择 H4/H5 Profile 或通过结构测试就代表真实/生产证据成立。
6. 现有 741 个测试足以替代真实非技术用户试用、长期运行和客户环境验收。
7. 当前未归档工作区修复已经成为稳定发行版本。

## 8. 对“这段时间到底完成了什么”的最终回答

与能力边界报告对照，**主要架构设想已经不再停留在报告里**：能力集合、运行闭包、外部契约、Builder 合同、分级评测、模块证据、三类界面、持久任务、Connector 硬控制和三个压力场景都已有真实代码和定向证据。就“能否根据报告逐步推进设想”这个问题，`v0.4.2` 至 `v0.4.10` 已经给出了肯定答案，而且没有把拿不到的 H4/H5 外部证据伪造为通过。

但产品层面的答案更克制：**Lilies 目前是一个架构能力很强、受控本地集成已经成形、普通用户体验仍在收口的试点平台。** 它已跨过概念演示阶段，却还没有跨过真实客户和生产运营阶段。下一步不应再继续无限扩展架构面，而应先完成当前可用性修复归档、失败验收自动修复全链路、新应用 Customer Runtime 全链路，然后选择一个被授权的真实 H4 试点场景，验证报告中一直诚实保留的外部证据缺口。

## 9. 关键证据索引

- 能力边界报告：`docs/lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx`
- 意图登记：`docs/evolution-control/report_intents.json`
- 阶段报告：`docs/stage-reports/v0.4.2_*` 至 `docs/stage-reports/v0.4.10_*`
- Codex-like 证据：`docs/workingon-archives/v0.4.4/v0.4.4_component_evidence.json`
- Capability Build Contract：`docs/workingon-archives/v0.4.5/v0.4.5_capability_contract_evidence.json`
- 模块与证据注册：`docs/workingon-archives/v0.4.6/`
- 三界面与 Governance：`docs/workingon-archives/v0.4.7/`
- Evaluation Harness：`docs/workingon-archives/v0.4.8/`
- Durable Job：`docs/workingon-archives/v0.4.9/`
- Connector / Embedding：`docs/workingon-archives/v0.4.10/`
- 当前维护者回放：`.tmp/human-maintainer-journey/run8/journey.json`
- 当前真实验收回放：`.tmp/human-acceptance-journey/run2/journey.json`
- 当前客户旅程未闭环证据：`.tmp/human-customer-journey/run9/journey.json`
