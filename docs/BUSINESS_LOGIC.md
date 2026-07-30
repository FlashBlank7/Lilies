# 业务逻辑与项目说明

本文档描述当前平台的业务设计、核心对象、运行生命周期和验收边界。它面向两类读者：

- 产品/运营视角：理解“用户给需求，平台如何产出可运行、可微调、可发布的智能体工作流”。
- 开发/维护视角：理解后端、前端、工作流、Agent Runtime、测试发布之间如何协作。

当前项目的定位不是“把需求一次性变成一段代码”，而是一个可视化智能体工作流平台：Claude Code 风格的智能体大脑负责理解、规划、使用工具、修复失败；Dify 风格的积木画布负责把能力变成可检查、可编辑、可发布的业务应用。

## 1. 产品目标

平台要解决的问题是：业务用户只描述需求，系统自动搭建一个可以真实运行的智能体应用，并且这个应用不是黑箱。

因此首期产品目标被拆成四个关键词：

1. 自动搭建：用户输入自然语言需求后，平台创建草稿，并由智能体团队增量添加积木、配置 Agent、连接变量、生成测试。
2. 可视化可编辑：最终产物是一张 Workflow，而不是一段隐藏代码。人可以继续拖拽、改配置、调 Prompt、改测试。
3. 真实验收：只有当前草稿对应的测试真实通过，才允许发布。Mock 只用于单元测试，不作为可用性验收。/ 想法本身没错，但是现在这个地方验收的测试用例是自己生成的，另外本身可能离客户的的需求就还有一定偏移，我觉得没必要在加这种限制。
4. 可复用交付：发布版本不可变，可以被外部调用，也可以作为另一个 Agent 或 Workflow 的工具。 / 这个表述比较模糊，其实就是发布的工作流在一个空间里面，目前这个空间没有特定的访问权限的说法因为目前还没有区分管理员和用户。现在核心逻辑还在开发阶段，只需要简单易用但是不要发生生成的工作流后台自动运行的现象就可以，工作流目前仅调试和发布测试复用的能力。

## 2. 业务角色

当前系统里有四类角色。它们不一定对应独立账号，而是业务逻辑中的职责边界。 / 就是不是独立账号，只是我们需要分工的智能体团队来完成任务

### 2.1 需求提出者

输入业务目标，例如：

> 每天早上 8 点联网搜索最近资讯，整理日本女性偶像团体活动、八卦和新消息，生成日报。

需求提出者关注结果是否可用，不需要理解底层节点和 Agent 配置。/ 产生对于需求对于客户可用的结果是智能体团队的目标，但是不能忽略积木工作流的可编辑性，因为客户或许还有后续调整和或者自己上手调整；

### 2.2 自动搭建团队

后端中的 Builder Team。它复用完整 Agent Runtime，不硬编码成固定的 Architect、Builder、Tester 代码分支，而是通过数据驱动的 Agent 定义来创建协调者和队友。

它的职责是：

- 拆解需求和验收项。
- 搜索积木目录。
- 逐步修改草稿，而不是一次性输出整张图。
- 创建或绑定 Claude Agent 节点。
- 生成真实测试。
- 运行、观察失败、修复配置。
- 满足发布门槛后发布版本。

### 2.3 人工编辑者

在 Studio 中查看和修改工作流的人。人工编辑者可以：

- 改节点配置。
- 改连接线。
- 改 Agent Prompt、工具、Skill、MCP。
- 增删测试。
- 将历史版本加载为新草稿继续修改。

人工修改后，草稿会被视为未验证，必须重新测试才能发布。

### 2.4 运行调用方

通过 UI、API 或另一个 Agent/Workflow 调用已发布应用。运行调用方默认绑定具体版本，因此后续编辑草稿不会影响线上运行。

## 3. 核心业务对象

### 3.1 Application

产品级应用入口。一个 Application 表示一个可交付的智能体应用，可以是：

- `workflow`：可视化 DAG 工作流。
- `chat`：面向对话的智能体应用。

Application 拥有一个可变 Draft，以及多个不可变 Version。

### 3.2 Draft

每个 Application 只有一个当前 Draft。Draft 是工作台上的可变版本，包含：

- WorkflowSpec。
- Agent 绑定。
- 测试集。
- 布局和变量。
- 当前 revision。
- 当前 content hash。
- 最近一次测试通过的 tested hash。

所有写操作都带 `expected_revision` 和 `idempotency_key`，避免多个智能体队友或前端操作互相覆盖。

### 3.3 ApplicationVersion

发布后生成的不可变快照，包含当时的：

- Workflow。
- Agent 配置。
- 测试记录。
- 依赖和内容 hash。

运行默认绑定 Version，不绑定 Draft。这样“线上版本”和“编辑中的版本”不会互相污染。

### 3.4 WorkflowSpec

WorkflowSpec 是工作流图。它包含：

- 节点。
- 边。
- 嵌套子图。
- 变量。
- 布局。
- 执行策略。

后端会编译 WorkflowSpec，检查端口、变量类型、不可达节点、非法环、分支和循环约束。

### 3.5 BlockDefinition

积木定义。每类节点都通过统一结构注册：

- 类型和版本。
- 分类和展示信息。
- JSON Schema 配置。
- 输入/输出端口。
- 重试、超时、错误分支能力。
- Secret/Credential 引用要求。
- 前端编辑器提示。
- 中英双语展示元数据。

积木目录是 AI 和前端共用的资源。自动搭建团队看到的“积木”，与用户在画布上看到的是同一套定义。

### 3.6 AgentSpec / AgentBinding

AgentSpec 是数据驱动的智能体定义，包含：

- 模型 profile。
- system prompt 和初始 prompt。
- 工具白名单/黑名单。
- Skill。
- MCP。
- 权限。
- 上下文、轮次和预算。
- 子智能体和隔离策略。

AgentBinding 用于把一个 Agent 绑定到 Claude Agent 节点。绑定可以是内联快照，也可以引用可复用 Agent 版本。

### 3.7 Build

Build 是一次自动搭建过程。它记录：

- 需求。
- 队友任务。
- 画布操作。
- 测试运行。
- 修复过程。
- 发布结果或 `needs_attention` 状态。

前端通过 SSE 实时看到团队如何添加积木、连线、运行和修复。

### 3.8 TestSuite / WorkflowTestCase

测试集是发布门槛的一部分。测试不仅检查最终文本，还检查结构证据和工具证据，例如：

- 必须包含哪些节点类型。
- 必须包含哪些 Tool 节点。
- 必须调用哪些工具。
- 最少工具调用次数。
- 是否需要引用来自工具的 URL。
- 是否需要特定输出字段或断言。

只有测试对应当前草稿精确 content hash 时，才允许发布。

### 3.9 Run

Run 是一次工作流执行记录。它包含：

- 输入。
- 节点级事件。
- thinking。
- 工具调用。
- 输出。
- 错误。
- 人工暂停与恢复状态。
- 费用和 token 统计。

Run 事件通过 SSE 输出，并持久化到 SQLite WAL 与 JSONL 事件文件，便于断线续传和重启恢复。

## 4. 主业务流程

### 4.1 从需求到发布

```text
自然语言需求
  -> 创建 Application Draft
  -> Builder Team 拆解验收项
  -> 搜索积木与 Agent 能力
  -> 增量添加节点和连线
  -> 配置 Agent、工具、Skill、MCP
  -> 校验 Schema / 变量 / 权限 / 图结构
  -> 生成测试
  -> 真实运行测试
  -> 根据失败记录修复
  -> 当前 hash 全部通过
  -> 发布不可变 Version
```

这个流程的关键约束是：Builder Team 不能直接输出一整份 Workflow JSON，也不能通过任意 Code 节点绕过积木系统。它必须像人一样使用受控 API 搭建画布。

### 4.2 手工编辑到再发布

```text
打开已发布版本
  -> 加载为 Draft
  -> 人工修改节点 / Prompt / 测试
  -> Draft 标记为未验证
  -> 重新运行测试
  -> 测试通过
  -> 发布新 Version
```

旧版本保持不变，新版本成为 active version。外部调用如果绑定旧版本，不会被新草稿影响。

### 4.3 工作流运行

```text
Run 输入
  -> 编译 WorkflowSpec
  -> 校验图结构和变量引用
  -> 执行 Start / Schedule
  -> 执行业务节点
  -> 分支、聚合、循环或暂停
  -> 输出 Answer / End
  -> 写入事件和最终结果
```

节点执行期间会持续产生事件。前端 Trace 面板和 SSE 客户端都能看到节点开始、完成、失败、工具调用、Agent thinking 和最终输出。

### 4.4 定时运行

Schedule Trigger 节点用于表达业务定时，而不是只依赖外部 cron。

当前逻辑是：

- 本地开发默认不启动后台调度器，除非显式设置 `SCHEDULER_ENABLED=true`。
- 使用配置中的 timezone 和 local time。
- 每个本地日期只触发一次 schedule fire。
- 如果进程重启时发现 queued/running Run，会标记为 interrupted。
- 调度器不能绕过任务监视器直接发起无限重试；任何恢复重试都必须先进入统一的运行监管、预算和取消策略。
- Studio 画布中的显式 run 与 Builder 任务绑定页面生命周期；离开画布时必须取消仍在运行或暂停等待输入的任务。

### 4.5 Workflow 作为工具

已发布 Workflow 可以注册为另一个 Workflow 或 Agent 的 Tool。这样可以形成复用层：

```text
底层 Workflow：资讯搜索与整理
  -> 发布为 Tool
  -> 上层 Agent 调用该 Tool
  -> 上层 Workflow 继续分发、审核或发送
```

## 5. 积木目录与节点语义

首期积木覆盖真实可执行的工作流场景：

- User Input / Start
- Schedule Trigger
- LLM
- Claude Agent
- Tool
- If / Else
- Question Classifier
- Parameter Extractor
- Template Transform
- Variable Assigner
- Variable Aggregator
- HTTP Request
- Iteration
- Loop
- Human Input
- End / Answer

### 5.1 Claude Agent 节点

Claude Agent 节点运行完整 AgentRuntime，支持：

- 工具。
- Skill。
- MCP。
- 子智能体。
- 上下文压缩。
- 权限隔离。
- 预算和轮次控制。

它适合负责复杂推理和多步工具任务。

### 5.2 Tool 节点

Tool 节点用于显式调用一个工具，例如 WebSearch、Read、Write、Edit、Bash 或已发布 Workflow。

业务上推荐把关键外部动作做成 Tool 节点，而不是全部藏进 Agent 节点内部。这样测试和人工审查能看到清晰证据。

当前 WebSearch Tool 直接走搜索实现，不创建 Docker 沙盒；文件和命令类工具仍走沙盒隔离。

### 5.3 分支与聚合

If / Else、Question Classifier 和 Parameter Extractor 用于结构化决策。Variable Aggregator 可以聚合多个上游输出，支持可选引用，因此不同分支只要有一个输出，也能继续汇总。

### 5.4 Human Input

Human Input 节点会持久化暂停状态。运行可以在进程重启后恢复，适合审批、补充表单和人工判断。

## 6. 发布与验收规则

发布不是简单保存，而是业务交付动作。当前发布必须满足：

1. Draft 图结构合法。
2. 节点配置符合各自 JSON Schema。
3. 变量引用只连接上游且类型兼容的输出。
4. 权限和工具配置通过检查。
5. 所有强制测试通过。
6. 测试记录对应当前 Draft content hash。

如果达到预算或修复上限仍未通过，Build 进入 `needs_attention`，保留团队上下文和失败证据，但不会发布失败版本。

## 7. 中英双语逻辑

平台目前支持中文和英文两套产品界面文本：

- 首页。
- AI Build Studio。
- 工作流编辑器。
- 测试/版本/运行 Trace 面板。
- 积木目录名称、描述和分类。

语言选择保存在浏览器 `localStorage` 中。积木定义由后端返回 `editor.i18n.zh` 和 `editor.i18n.en`，因此 AI 搭建团队和前端编辑器使用同一份积木元数据。

注意：双语能力覆盖平台 UI 和积木目录。用户写入的需求、Prompt、节点标题、测试内容和运行输出会保持原文，不会被系统自动翻译，以免改变业务语义。

## 8. 示例交付：日本女性偶像日报

当前验证项目是“每天 8am 搜索最近资讯并整理日本女性偶像团体活动、八卦、新信息的新闻智能体”。

它的业务结构如下：

```text
Schedule Trigger，08:00 Asia/Tokyo
  -> 5 个 WebSearch Tool 节点
  -> Variable Aggregator 汇总搜索证据
  -> Claude Agent 生成日报草稿
  -> Parameter Extractor 抽取结构化摘要
  -> If / Else 判断是否包含八卦/传闻
  -> Template Transform 格式化日报
  -> 分支 Variable Assigner
  -> Variable Aggregator 汇总最终结果
  -> Answer 输出
```

当前已发布版本：

- Application：`日本女性アイドル Daily`
- Application ID：`b86beecf-2567-40c9-a71c-9f49f8ff046e`
- Active Version：`4`
- Content Hash：`7dc4ba49b1f151b5d86bcda29a063d5be8db82fe0a4add32ef08b50651383642`

这个示例的价值在于它不是只有一个 Agent 方块，而是显式暴露了搜索、聚合、Agent 整理、结构抽取、分支和输出节点，便于人工继续微调。

## 9. 前端页面逻辑

原创 Next.js Studio 当前包含：

- 应用列表与自然语言创建页。
- AI Build Studio：需求、构建任务、实时画布、测试和发布状态。
- Workflow Editor：拖拽、连线、节点配置、变量选择。
- Agent/Prompt/Skill/MCP 微调入口。
- 版本历史、发布控制和加载为草稿。
- Workflow 调试运行、节点 Trace、工具证据和 Human Input 恢复。
- 中英双语切换。

前端通过 API 读取积木目录，不在本地硬编码节点能力。这样后端新增积木后，前端可以读取 Schema 和端口信息进行展示与编辑。

## 10. 后端模块分工

```text
platform/backend/src/agent_platform/api.py
  FastAPI 入口、鉴权、SSE、调试页面。

platform/backend/src/agent_platform/applications.py
  Application、Draft、测试、发布和增量修改业务逻辑。

platform/backend/src/agent_platform/blocks.py
  积木定义、Schema、端口、分类和双语元数据。

platform/backend/src/agent_platform/builder.py
  自动搭建团队、任务依赖、mailbox、测试修复和发布决策。

platform/backend/src/agent_platform/runtime.py
  Claude Code 风格 Agent loop、工具调用、上下文压缩和子智能体。

platform/backend/src/agent_platform/workflow_runtime.py
  DAG 编译、节点执行、分支、循环、Human Input、运行事件。

platform/backend/src/agent_platform/workflow_storage.py
  Application、Draft、Version、Build、Run 和 ScheduleFire 持久化。

platform/backend/src/agent_platform/providers/
  供应商无关 ModelProvider 接口和 DeepSeek 适配器。

platform/backend/src/agent_platform/tools/
  Read、Write、Edit、Glob、Grep、Bash、WebSearch、MCP 和 Workflow Tool。

platform/frontend/
  Next.js + React Flow Studio。
```

## 11. 运行与运维逻辑

本地开发标准入口：

```bash
./scripts/dev_platform.sh
```

默认地址：

- API：`http://127.0.0.1:8001`
- Studio：`http://127.0.0.1:3000`

关键环境变量：

- `DEEPSEEK_API_KEY`：DeepSeek 密钥，只从环境读取，不写入数据库或事件。
- `API_TOKEN`：本地 API Bearer Token。
- `WORKSPACE_ROOT`：沙盒可访问工作区根目录。
- `WORKSPACE_HOST_ROOT`：API 在 Docker 中运行时，对应宿主机目录。

持久化：

- SQLite WAL 保存配置和状态。
- JSONL 保存可回放事件。
- Docker sandbox 隔离工具执行。

## 12. 当前边界与后续方向

当前系统已经形成“需求 -> 积木工作流 -> 真实测试 -> 发布 -> 运行”的闭环，但仍有一些明确边界：

- 首期不开放任意 Code 节点，避免 AI 绕过积木系统。
- `allowlist` 网络策略已进入配置，但 Docker 层域名级过滤仍需接入 egress proxy。
- 当前 WebSearch 基于搜索/RSS 能力，新闻质量还可以继续接入更稳定的数据源。
- 生产部署需要进程管理、日志聚合、备份策略和外部队列。
- 前端双语覆盖产品界面和积木目录，业务内容翻译需要作为单独工作流能力实现。

推荐的下一阶段目标：

1. 强化 Agent 节点配置编辑器，完整暴露工具、Skill、MCP、权限和预算。
2. 增加测试用例编辑器，让业务人员能直接修改验收项。
3. 增加版本差异视图，展示节点、边、配置和 Agent Prompt 的变化。
4. 将已发布 Workflow Tool 做成前端可选择资源。
5. 为定时任务增加可视化运行日历和失败重试面板。
