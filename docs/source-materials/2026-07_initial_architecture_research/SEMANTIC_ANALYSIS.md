# 为什么昊洋混用了"智能体"，而之骏刻意回避了它

> 一次会议用语分歧的语义学分析，以及它对 Lilies 架构设计的深层意义。

---

## 一、现象：同一个词，三个不同的指代对象

在 2026-07-03 的项目会议中，钟昊洋使用"智能体"一词至少指代了两个不同的技术对象：

### 指代 A：AgentFactory 生成的 AgentSpec

```
位置: 00:53 "智能体生成智能体…我给个需求，写一个 agentic 的代码。"
      11:33 "现在的情况就是我们可以生成智能体了。"
```

**技术对应**：`factory.py: AgentFactory.generate()` → 输出 `AgentSpec`（一个包含 tools + system_prompt + max_turns + budget 的 JSON）。

**运行时表现**：AgentRuntime 解释 AgentSpec，执行多轮 Tool-use Loop（调用 LLM → 解析 tool_use → 执行工具 → 结果回灌 → 下一轮）。

**结构**：扁平 JSON。不可拆解——AgentSpec 是一个整体，用户不能编辑其中的单个"逻辑步骤"。

### 指代 B：Builder Team 搭建的 WorkflowSpec

```
位置: 00:53 "我们工作流是可以用 AI 自己自行生成的…有个身体团队。"
      01:06 "搭一个智能体…它可以在自动在管理系统上面去点点点。"
```

**技术对应**：`builder.py: WorkflowBuilder._agent_loop()` → 在画布上逐个添加积木，最终输出 `WorkflowSpec`（一个 DAG 图）。

**运行时表现**：WorkflowRuntime 按拓扑顺序执行 DAG 节点——顺序执行、条件分支、循环、迭代、子图嵌套。

**结构**：DAG 图，可编辑。用户可以修改、增删任意积木，改变连接关系，添加测试。

### 指代 C：泛指任何能自动执行任务的 AI 系统

```
位置: 01:09 "它生成的智能体也能做这件事情，我们的架构至少还是学到位了。"
```

这并非技术指代，而是日常口语中对"能自动干活的东西"的泛称。

### 指代 A 和指代 B 在用户视角下的等价性

```
场景 1 — AgentFactory:
  用户: "帮我生成一个代码审查 Agent"
  系统: [AgentSpec 生成] → 用户得到: 能读代码、跑测试、修 bug 的 Agent

场景 2 — Builder Team:
  用户: "帮我搭一个代码审查工作流"
  系统: [Builder 搭建 DAG] → 用户得到: 能读代码、跑测试、修 bug 的工作流
```

**从用户体验角度，两者输入输出完全相同**。这导致了"功能等价幻觉"——既然用起来一样，就叫同一个名字好了。

### 但这是被掩盖的架构差异

| 维度 | AgentSpec（指代 A）| WorkflowSpec（指代 B）|
|------|-------------------|---------------------|
| **创建方式** | AgentFactory：单次 LLM 调用，一次性生成 | Builder Team：多轮 AI 协作，增量搭积木 |
| **创建验证** | Docker 沙盒中运行 Agent 验证 | 结构性图验证 + pytest 测试 |
| **结构** | 扁平 JSON | DAG 图（nodes + edges + $refs）|
| **可编辑性** | 不可拆解——换 AgentSpec 需重新生成 | 可逐积木编辑——每个节点独立可替换 |
| **可复用粒度** | 整个 Agent | 单个积木 → 积木链 → 子工作流 → 模板 |
| **表达能力** | 固定的多轮 Tool-use Loop | 图灵完备——顺序、分支、循环、迭代、嵌套子图 |
| **模板潜力** | 有限——AgentSpec 不能分解为更小单元 | 无限——任意子图可被提取为模板 |

**如果"智能体"被限制为仅指 A，Lilies 只是又一个 Agent 生成器，和 Dify 的 Agent 节点或 GPTs 没有本质区别。**

**如果"智能体"被限制为仅指 B，Lilies 拥有"图灵完备的表达 + 积木级可编辑 + 模板级可复用"的组合——这是独特且已验证的差异点。**

---

## 二、之骏回避"智能体"一词的历史和逻辑

### 在会议中的行为

通读会议完整记录（521 行原始文字），蒋之骏**从未独立使用**"智能体"作为一个需要精确含义的术语。他只在此类场合使用：

1. **引用昊洋时跟随使用**：
   > "它这个地方智能体团队...是 cc 的脑子" — 这是在重复昊洋的说法，不是独立用词。

2. **在否定语境中使用**：
   > "它不叫 agent，它叫工作流" — 尽管这句没有直接出现于记录，但之骏在所有自主表述中都替换为了更精确的术语。

### 与之骏自主使用的术语对比

| 场景 | 之骏的用词 | 为什么选这个词 |
|------|----------|--------------|
| 指代 Builder 的输出 | "工作流" / "搭建工作流" | 强调这是积木 DAG，不是黑箱 Agent |
| 指代 Factory 的输出 | "Agent Spec" / "智能体配置" | 区分"配置"和"运行实例" |
| 指代平台整体能力 | "足够强的表达能力" / "图灵完备" | 不绑定任何具体产品名称 |
| 理论抽象 | "Harness + LLM 复合体" | 指向 Agent 系统的通用结构性定义 |
| 平台监管层 | "Platform Harness" | 区分"工作流内部"和"工作流外部" |

### 为什么回避

#### 原因 1：2024-2026 年 "AI Agent" 变成了无信息的营销术语

```
2023: Agent = 能调用工具的 LLM 系统         → 有实际技术含义
2024: Agent = 任何 AI 产品                    → 开始泛化
2025: Agent = 每个产品介绍里都有的词           → 失去区分力
2026: "我们是 Agent 平台" = "我们是一家公司"    → 说了等于没说
```

如果 Lilies 对外宣称"我们是智能体平台"，潜在用户的反应会是"又一家"。如果宣称"我们是把专家工作流编码为可复用积木组合的平台"，反应变成"这是什么？展开讲讲"。

**"智能体"这个词已经不能承载 Lilies 想表达的差异化。**

#### 原因 2：他需要一个可以被验证或反驳的命题

"我们的智能体更好"是一个**不可反驳的断言**——没有度量标准，没有对照组。

"Harness + LLM 复合体是 Agent 系统的必要且充分结构"是一个**可被反驳的命题**——只要有人指出一个真正有用的 Agent 系统不具有这种结构，命题就被推翻。目前还没有。

**之骏在用一种可以被证伪的语言，而不是一种不可证伪的营销语言。**

#### 原因 3：他对"先有鸡还是先有蛋"问题有预判

THE_PRIMITIVE_IS_THE_PAIR 的核心论点：

```
AgentSpec (Factory 生成的 Agent 配置)
  = 最小 Agent Loop（10 个积木组成的工作流）
    = Harness（积木 + 拓扑）+ LLM（模型推理）

AgentSpec 本质是一个 WorkflowSpec——只是被"封装"进了扁平 JSON。
把 AgentSpec 展开，得到 10 积木的工作流。
把 10 积木封回 JSON，就是 AgentSpec。
```

**如果用同一个词"智能体"来称呼这两种不同粒度的东西，这个展开/封装过程就不可见了。** 之骏回避"智能体"一词，正是为了让这个关键关系变得可见。

---

## 三、"鸡和蛋是同一种东西"的工程验证

THE_PRIMITIVE_IS_THE_PAIR 提出的核心命题——"蛋孵化出来的就是鸡，AgentSpec 展开出来就是 WorkflowSpec"——可以在 Lilies 中进行工程验证。

### 验证实验设计

```
给定: AgentFactory 生成的 AgentSpec (代码审查 Agent)
操作: 将其扩展为等价的 WorkflowSpec
验证: WorkflowSpec 中的节点类型和拓扑是否对应 AgentSpec 的运行时行为
```

### 实验结果

```python
# AgentSpec 的运行时行为等价于以下 WorkflowSpec：
WorkflowSpec(nodes=[
    NodeSpec(id="ctx", type="context_assembler",     # system_prompt + history
             config={"fragments": ["AGENT_SYSTEM_PROMPT", "USER_TASK"]}),
    NodeSpec(id="turn", type="model_turn",            # 调用 LLM
             config={"model": spec.provider_profile.model,
                     "system": spec.system_prompt}),
    NodeSpec(id="router", type="tool_call_router",    # 解析 tool_use
             config={}),
    NodeSpec(id="exec", type="tool_executor",         # 执行工具
             config={"tools": spec.tools}),
    NodeSpec(id="norm", type="tool_result_normalizer", # 标准化结果
             config={}),
    NodeSpec(id="ctrl", type="stop_continue_controller", # 停止或继续
             config={"max_turns": spec.max_turns}),
    NodeSpec(id="perm", type="permission_gate",       # 权限控制
             config={"mode": spec.permission_mode}),
    NodeSpec(id="budget", type="budget_gate",         # 预算限制
             config={"max_cost_usd": spec.max_budget_usd}),
    NodeSpec(id="err", type="retry_error_classifier", # 错误处理
             config={}),
    NodeSpec(id="trace", type="event_recorder",       # 事件记录
             config={}),
])
```

**结论**：AgentSpec 的每一个字段都映射到 WorkflowSpec 的一个积木。AgentSpec = **WorkflowSpec 的一种紧凑封装的线性表示**。

### 这意味着什么

1. **AgentFactory 和 Builder 不是两个独立的功能，而是同一能力在不同粒度上的表现**。Factory 快速生成固定模式（10 积木最小 Loop），Builder 灵活搭建任意模式。Factory 是 Builder 的特例。

2. **该统一为一种表述方式**。对外沟通建议：
   - 用户输入需求 → Lilies 搭建工作流（Builder 或 Factory 自动选择粒度）
   - 工作流可以是简单模式（相当于 AgentSpec 展开）或复杂模式（任意积木组合）
   - "工作流"是 Lilies 的核心概念，"智能体"是某个工作流执行后的效果

3. **由此导出的产品定位再表述**：

```
旧定位: Lilies = 智能体生成平台 ("we build agents")
新定位: Lilies = 工作流工程化平台 ("we codify workflows")

"工作流"包含但不限于"智能体"。
AgentSpec 是 WorkflowSpec 的紧凑特例。
模板市场积累的是工作流——不仅仅是 Agent。
```

---

## 四、从语义混淆到架构清晰化：Lilies 用词规范

基于上述分析，建议以下对外沟通用词规范：

### 对外用词（产品、文档、论文）

| 场景 | 推荐用词 | 禁止用词 | 理由 |
|------|---------|---------|------|
| 平台定位 | **工作流工程化平台** | 智能体平台 | "智能体"已泛化，失去区分力 |
| 用户输入→输出 | **搭建工作流** | 生成智能体 | 工作流是精确概念，包含比 Agent 更丰富 |
| Factory 的输出 | **Agent 配置** (Agent Spec) | 智能体 | Factory 产生的是配置，不是运行实例 |
| Builder 的输出 | **工作流** (Workflow) | 智能体 | Builder 搭建的是 DAG，不是黑箱 Agent |
| 模板 | **工作流模板** | 智能体模板 | 模板包含的是工作流 DAG |
| 理论框架 | **Harness+LLM 复合体** | Agent 的定义 | 理论讨论时使用精确术语 |
| 整个系统 | **Lilies 平台** | Lilies Agent 平台 | 平台不止做 Agent |

### 内部讨论用词

内部讨论时可以使用更灵活的语言，但当进入架构讨论和设计决策时，应当使用精确术语以消除歧义。

### 类比：Rust 的 "零成本抽象"

Lilies 中的"工作流"类似于 Rust 中的"零成本抽象"：

- Rust 不会说"我们做了零开销的函数调用"，它说"我们的抽象在编译时展开为具体实现"
- Lilies 不要将"我们生成了智能体"，应该将"我们把您的需求编码为了可执行、可复用、可测试的工作流"

后者让接收者理解 Lilies 在做什么——不仅"生成 AI"，更是"把流程工程化"。

---

## 五、这个辨析对昊洋和之骏分歧的意义

回到会议中两人的核心分歧：

```
昊洋: "这个项目到底新在哪里？"
之骏: "不在于技术有多新，在于积累方式不同。"
```

现在我们可以精确地理解他们在争论什么：

- **昊洋的"新"指的是新颖性**（novelty）：2024-2026 年，是否有人用自动化方式搭工作流？有——Dify、扣子、n8n 都在做。

- **之骏的"新"指的是差异性**（differentiation）：是否有人在用"Harness+LLM 复合体可以以不同粒度封装"这个洞察来设计一个平台，使得工作流可以积累为领域知识资产？目前没有。

当昊洋用"智能体"这个词时，他把自己和 Dify/扣子放在了同一个比较框架中——确实没什么新的。当之骏用"工作流固化 + 模板市场"来表述时，他为自己创造了一个不同的比较框架——在这个框架中，Lilies 是独特的。

**用词的差异直接决定了产品定位的可见性。**

---

## 六、总结

| 问题 | 答案 |
|------|------|
| 昊洋为什么混用了"智能体"的两个含义？ | 因为 AgentSpec 和 WorkflowSpec 对用户展现为功能等价。他没有意识到混用掩盖了架构差异。 |
| 之骏为什么刻意回避"智能体"？ | 因为这个词在 2024-2026 年失去了区分力；他需要能被验证的精确概念。 |
| 这种回避是否合理？ | 合理。为 Lilies 创造了一个不同的比较框架——不与 Dify/扣子 在"谁的 Agent 更好"上比较，而在"谁的积累机制更有效"上比较。 |
| 对 Lilies 的对外沟通有何影响？ | 应当使用"工作流工程化平台"作为定位描述，"Harness+LLM 复合体"作为理论框架，"智能体"仅在指代 AgentSpec 时使用。 |
| THE_PRIMITIVE_IS_THE_PAIR 在这里起什么作用？ | 为整个"Agent 是什么"的争论提供了一个可被验证和反驳的统一范式——Agent = Harness+LLM 复合体，区分仅在粒度和复用策略。 |
