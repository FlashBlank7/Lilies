# Lilies 公理的工程等价性分析

## 公理选择作为保障选择器

**前置**：[the_pair_categorical.tex](./the_pair_categorical.tex) · [asset_the_pair_formal_system.md](../../intellectual-assets/asset_the_pair_formal_system.md)

**关联**：[plan_categorical_theory_driven_engineering_v1.md](../../workingon/plan_categorical_theory_driven_engineering_v1.md)

---

## 摘要

The Pair 范畴论形式化建立在三条公理之上。每条公理被拒绝时，对应的 Agent 平台会获得特定的工程便利，同时失去特定的结构保障。本文不预设"满足全部公理就是更好"的立场，而是诚实分析：

1. 每条公理被拒绝时，对方获得了什么真实收益
2. Lilies 在公理约束下能否等价实现那些收益
3. 如果不能完全等价，差距在哪里，是否可被替代模式补偿
4. 如何将三条公理理解为"保障选择器"——团队可以根据自身需求选择接受哪些公理，清醒地理解对应的收益和代价

本文的分析方法是一个通用框架：**公理满足矩阵 → 结构推论 → 工程等价性评估**。任何 Agent 平台都可以放入这个框架中进行定位和比较。

---

## 第一章：公理回顾与工程解释

### 1.1 三条公理的工程转译

范畴论文档中的三条公理以数学语言陈述。以下给出每条公理的工程等价表述：

**公理 1（能力二分）**：
> 每个能力恰好是确定性的或非确定性的。不存在"模糊地带"。

**工程转译**：平台的每个原子构件要么产生可测试的输出（确定性），要么产生不可完全测试的输出（非确定性，因为涉及 LLM 推理）。不存在"部分可测试"的灰色构件——如果有，那说明该构件内部隐藏了未声明的 LLM 调用。

**公理 2（不可约性）**：
> 不存在包含所有实用能力的更小满子范畴。

**工程转译**：不能用"更聪明的构件"来替代"更多构件通过组合产生的涌现能力"。不存在一个"万能积木"可以取代所有其他积木的组合——任何试图创建万能积木的尝试只是在更细粒度上重建了组合结构。

**公理 3（Pair 幂等）**：
> $P^2 \cong P$。将 Pair 分解两次等价于分解一次。

**工程转译**：Agent 的输出是工作流运行结果，不是新的 Agent 定义。不存在"Agent 创建 Agent，该 Agent 又创建 Agent"的无限递归——层级在两层嵌套后饱和，不再产生新的结构类型。

### 1.2 公理作为"保障选择器"

```
公理接受的直接效果：

公理 1 → 每个构件可判定为 Det 或 NonDet
       → 确定性组合的自动验证
       → 非确定性构件的显式隔离

公理 2 → 构件的职责边界清晰
       → 不出现"N 个构件做同一件事"的碎片化
       → 构件数量有自然收敛趋势

公理 3 → 架构层级有理论上界（≤ 3 层）
       → 不存在"20 层嵌套 Agent"的调试噩梦
       → 结构在两层内饱和

公理拒绝的直接效果：

反公理 1 → "模糊构件"允许快速原型（一个节点做很多事）
          → 代价：无法静态验证，测试不可穷尽

反公理 2 → "智能节点"降低入门门槛
          → 代价：构件职责重叠且不可组合推理

反公理 3 → 递归 Agent 嵌套实现组织层级自然映射
          → 代价：调试跨越多层，无法定义完成条件
```

---

## 第二章：公理 3 的拒绝——嵌套 Agent

### 2.1 对方获得了什么

AutoGen 的嵌套 GroupChat 和 LangGraph 的递归 Supervisor 模式拒绝了 $P^2 \cong P$。它们允许：

**收益 A：组织层级的直译映射**

```
人类组织：
  产品总监
    ├── 前端组长
    │     ├── 开发者 A
    │     └── 开发者 B
    └── 后端组长
          ├── 开发者 C
          └── 开发者 D

AutoGen 直译：
  DirectorAgent
    ├── FrontendGroupChat
    │     ├── DeveloperAgent("A")
    │     └── DeveloperAgent("B")
    └── BackendGroupChat
          ├── DeveloperAgent("C")
          └── DeveloperAgent("D")
```

每一层的人类汇报关系直接映射为一层 Agent 嵌套。对于需要审计的合规场景，这种"结构同构"有沟通价值——非技术人员可以直观理解系统结构。

**收益 B：动态子团队的运行时创建**

```
AutoGen 模式：
  Orchestrator LLM 判断："这个合同审查需要法务 + 合规 + 财务三个视角"
  → 运行时动态创建 LegalReviewGroupChat(FaultAgent, ComplianceAgent, FinanceAgent)
  → 三个 Agent 进行多轮对话，互相质疑和补充
  → 输出综合审查报告
  → 子 GroupChat 销毁
```

子团队的成员组合和对话轮次完全由 LLM 在运行时决定，不需要设计者预先枚举所有可能的子团队类型。

**收益 C：子团队内部的多 Agent 自由对话**

```
AutoGen GroupChat 内部：
  Agent_A: "我认为合同第 3 条有合规风险，因为..."
  Agent_B: "不同意，根据最新法规 X，第 3 条是合规的。参考数据..."
  Agent_C: "B 的法规引用是对的，但我发现第 5 条有潜在问题..."
  Agent_A: "重新审视后，我撤回对第 3 条的担忧，但我补充第 7 条..."
```

这是一个涌现的对话过程——Agent 之间互相质疑、补充、修正。对话的轨迹不是预先编程的，而是从 LLM 的多轮交互中自然产生的。

### 2.2 Lilies 的等价实现

#### 2.2.1 组织层级 → cluster pub/sub + 独立 BlockFlow（✅ 等价）

```
Lilies 实现——静态层级委派：

┌──────────────────────────────────────────────┐
│  Director BlockFlow                           │
│                                              │
│  llm("分析任务需求，确定需要哪些子团队")      │
│    → if_else(需要前端: cluster_publish(       │
│        "task.frontend", {spec}))              │
│    → if_else(需要后端: cluster_publish(       │
│        "task.backend", {spec}))               │
│  cluster_subscribe("result.>", timeout=300)   │
│    → variable_aggregator(合并子团队结果)       │
│    → llm("综合所有结果，产出最终报告")         │
│    → end                                      │
└──────────────────────────────────────────────┘
        │ publish                    │ subscribe
        ▼                           ▼
┌──────────────────┐     ┌──────────────────┐
│ FrontendTeam     │     │ BackendTeam      │
│ BlockFlow        │     │ BlockFlow        │
│ (Template)       │     │ (Template)       │
│                  │     │                  │
│ 等价于 AutoGen   │     │ 等价于 AutoGen   │
│ 的子 GroupChat   │     │ 的子 GroupChat   │
└──────────────────┘     └──────────────────┘
```

**关键差异**：AutoGen 的层级结构在运行时由 LLM 动态创建，Lilies 的层级结构在编译时由 DAG 声明。但在表达能力上——"一个协调者将任务分派给子团队并收集结果"——两者完全等价。

**L1 验证数据**（来自 [plan_categorical_theory_driven_engineering_v1.md](../../workingon/plan_categorical_theory_driven_engineering_v1.md) Task Market 实验）：
- 4 个 Producer + 8 个 Worker，120 轮交互，287 次任务发布，63 次认领
- 8 个 Worker 的认领分布标准差 < 0.5（无需中心调度器的均匀负载均衡）
- 涌现 37 个模式（hot_resource × 1, starvation × 8, ping_pong × 28）
- 所有交互可仅用 $L_1 = \{\mathrm{publish, subscribe, acquire, release}\}$ 表达

#### 2.2.2 动态子团队创建 → 参数化通用模板 + iteration（⚠️ 近似）

```
AutoGen 动态创建 vs Lilies 方案 B（参数化）：

AutoGen:
  LLM: "需要法务+合规+财务"
  → 动态创建 GroupChat([LegalAgent, ComplianceAgent, FinanceAgent])

Lilies 方案 B（参数化通用模板）:
  llm: "需要法务+合规+财务"
  → if_else(需要子团队) →
    cluster_publish("task.subteam", {
      participants: ["legal_expert", "compliance_expert", "finance_expert"],
      topic_prefix: "subteam.contract_review"
    })

  一个通用的 DynamicSubTeam BlockFlow subscribe "task.subteam"
  → 读取 participants 列表
  → iteration(participants, parallelism=3)
      内部每个 iteration：
        llm("你扮演{participant}角色，分析{task}")
        → cluster_publish("subteam.{topic}.opinion", {role, analysis})
  → cluster_subscribe("subteam.{topic}.opinion", timeout=60)
  → llm("综合各方意见，输出共识报告")
```

**等价性评估**：参数化通用模板可以实现"运行时决定参与者组合"，但不能实现 AutoGen GroupChat 中的"多轮自由对话"。在 Lilies 方案中，每个角色独立产出一份分析，最后由一个综合 LLM 生成共识报告——这是"并行分析 + 汇总"模式，而非"多轮互相质疑和修正"模式。

#### 2.2.3 多 Agent 自由对话 → loop + 多轮 pub/sub（⚠️ 可实现但非原生）

```
Lilies 实现多 Agent 自由对话：

loop(
  break_condition: {operator: "equals", expected: "consensus"},
  break_value: $ref(consensus_check, decision),
  max_iterations: 10,
  workflow: {
    iteration(participants, parallelism=3) {
      llm("你是{role}。阅读讨论历史{discussion_history}。"
          "如果需要修改之前的观点，请说明理由。"
          "如果同意之前某人的观点，请明确表示。")
      → cluster_publish("discussion.{round}", {role, opinion})
    }
    → cluster_subscribe("discussion.{round}")
    → llm("判断是否达成共识")
    → end
  }
)
```

**代价**：
- 上述结构需要在 BlockFlow 中**显式编排**（约 8-10 个积木），而 AutoGen 的 GroupChat 是一个**原生结构**（一行代码）
- 共识检测逻辑需要手动设计（AutoGen 的 GroupChat 有内置的 speaker selection 和终止条件）
- 对话历史管理需要显式的 `variable_aggregator` + `conversation_memory`

**但是**：一旦这个模式被封装为 Template，后续使用时就是一次 Template 展开——与 AutoGen 的一行代码等价。

### 2.3 深层分析：多层嵌套是否必要

AutoGen 和 LangGraph 允许任意深度的嵌套。但实践中：

| 嵌套深度 | 人类组织等价 | 信息衰减 | AutoGen 社区中的实际用例 |
|---------|-----------|---------|---------------------|
| 1 层（Agent → LLM） | 个人决策 | 无 | 100% 的用例 |
| 2 层（Orchestrator → Team） | 组长 → 组员 | 低 | 常见（约 60-70% 的多 Agent 用例） |
| 3 层（Director → Manager → Workers） | 总监 → 组长 → 组员 | 中等 | 偶尔（约 10-15%） |
| 4 层+ | CxO → VP → Director → Manager → IC | 严重 | 极少，且往往伴随"为什么不合并这两层"的重构 |

**Lilies 的不动点定理（Thm 7.1）** 证明 level_3 = level_2。结合上述经验数据，一个合理的假设是：**3 层以上不产生新的语义能力，只增加协调开销**。Lilies 的 3 层上限（block → workflow → cluster）恰好覆盖了 99% 的实际用例。

如果确实需要表示 4 层以上的组织结构，Lilies 的策略是**展平**：将 4 层映射为 3 层内的更多并行子团队，而非增加第 4 层。

---

## 第三章：公理 2 的拒绝——"智能积木"

### 3.1 对方获得了什么

Dify 的核心策略是让单个节点变得更智能——`if_else` 内置 LLM 路由、参数提取器内置语义理解、代码节点允许嵌入式脚本。

**收益 A：极低的使用门槛**

```
Dify 用户的心智模型：
  "我需要根据用户输入的内容做不同的事情"
  → 拖一个"智能分类"节点
  → 告诉它可能的类别
  → 完成

Lilies 用户的心智模型：
  "我需要根据用户输入的内容做不同的事情"
  → 拖一个 llm 节点（配置系统提示和结构化输出）
  → 拖一个 if_else 节点（配置条件分支）
  → 连接它们
  → 完成
```

Dify 的"一个节点"对等于 Lilies 的"一个便利积木"（`question_classifier` 正是 `llm + if_else` 的封装）。真正的区别在于：**Dify 的底层不承诺"智能分类"可以分解**——它的内部结构对用户是不透明的。Lilies 的 `question_classifier` 对高级用户是透明和可分解的。

**收益 B：快速原型时的便利**

当用户不知道最终设计时，Dify 的"先跑起来再说"策略有优势：

```
Dify 快速原型：
  拖 5 个智能节点 → 跑通 → 逐步优化单个节点的 prompt

Lilies 快速原型：
  Builder Team 根据需求描述生成 BlockFlow → 跑通 → 
  如果某个节点有问题，展开它，修改内部的组合结构
```

### 3.2 Lilies 的等价实现（✅ 等价）

**判定为等价**的原因：Lilies 有完整的机制覆盖 Dify 的所有"智能节点"功能：

| Dify 智能节点 | Lilies 等价积木 | 实现方式 |
|-------------|--------------|---------|
| 智能分类 | `question_classifier` | 封装 `llm(prompt="classify...") → if_else` |
| 参数提取 | `parameter_extractor` | 封装 `llm(structured_output=schema)` |
| 代码执行 | `tool` + sandbox | 通过 `tool_executor` 在 sandbox 中运行 |
| LLM 对话 | `llm` / `model_turn` | 原生积木，与 Dify 等价的单次调用 |
| 知识库检索 | `context_assembler` + `tool` | 组合实现，需要显式配置 |

### 3.3 诚实代价

**Lilies 无法完全等价的是"零设计成本的随意组合"**。

Dify 允许用户在不理解 Harness+LLM 拆分的情况下，凭直觉搭建工作流。这种自由在某些场景下确实有价值——特别是当用户是领域专家而非技术专家时。

Lilies 的缓解策略：
- **Builder Team**：终端用户用自然语言描述，Builder 负责设计积木组合
- **Template 市场**：常见模式预封装，一键使用
- **高级用户的透明度**：当模板不够用时，可以深入内部修改

---

## 第四章：公理 1 边界的模糊——任意图结构

### 4.1 对方获得了什么

LangGraph 允许任意有向图（含隐式环），不区分"DAG 中的边"和"循环回边"。

**收益 A：流程图的自然直译**

```
业务专家在白板上画的：
  接收订单 → 检查库存 → {库存不足 → 通知采购 → 回到检查库存}
                        → {库存充足 → 确认订单 → 发货}

LangGraph 代码（几乎 1:1 翻译）：
  graph.add_edge("receive_order", "check_inventory")
  graph.add_conditional_edges("check_inventory", decide, {
    "insufficient": "notify_procurement",
    "sufficient": "confirm_order"
  })
  graph.add_edge("notify_procurement", "check_inventory") // 回边！隐式循环
  graph.add_edge("confirm_order", "ship")

Lilies 等价（需要识别循环结构）：
  start → llm("提取订单信息") → 
  loop(
    break: stock >= required,
    workflow: {
      llm("检查库存") →
      if_else(库存不足:
        template_transform("通知采购") →
        variable_assigner("采购完成，回到检查"),
        库存充足:
        end
      )
    }
  ) → llm("确认订单") → llm("发货") → end
```

**收益 B：运行时由 LLM 决定图中的每一步走向**

LangGraph 的条件边可以完全由 LLM 输出决定——不需要预先枚举所有可能的分支值。

### 4.2 Lilies 的等价实现（✅ 等价，但需显式设计）

| 能力 | 等价性 | 实现 |
|------|:---:|------|
| 图灵完备性 | ✅ | DAG + 显式 loop 证明与任意有向图等价（Habel & Plump 2001） |
| 隐式循环 | ⚠️ | `loop` 积木需要显式声明 break condition，不如隐式回边自然 |
| LLM 驱动的条件路由 | ✅ | `if_else` 的分支选择可以是 `$ref(llm_output)` |
| 运行时动态修改拓扑 | ❌ | 预置路径替代 |

### 4.3 "动态修改图拓扑"的深度分析

这是 Lilies 最明确的结构性限制。LangGraph 的 LLM 可以在运行时决定"插入一个新节点"。

**Lilies 的等价策略**：把所有可能需要插入的节点预先放入 DAG，让 LLM 在运行时选择激活哪些：

```
LangGraph 动态插入：
  运行时 LLM 决定：需要增加验证步骤
  → 在图中动态插入 validation_node

Lilies 预置路径：
  编译时声明所有可能的路径：
    if_else(LLM判断: 是否需要验证?
      是 → validation_node → 继续
      否 → 直接继续
    )
```

**覆盖范围**：
- "已知的未知"（我们预见到可能需要验证）→ ✅ 预置路径完全覆盖
- "未知的未知"（LLM 提出一个设计者完全未预料的新步骤）→ ❌ Lilies 无法处理

**公平的评估**："未知的未知"在 LangGraph 实践中出现的频率是多少？多数 LangGraph 项目不会在运行时修改图拓扑——这个能力在文档中存在但在生产代码中罕见。Lilies 的判断是：这个能力带来的风险（绕过测试门禁、引入未验证的执行路径）超过其收益。如果确实需要动态生成新的工作流——那是 Builder Team 的职责，属于编译时而非运行时。

---

## 第五章：公理选型框架

### 5.1 三条公理作为三个独立的"选择开关"

```
公理接受矩阵 (Lilies = 全部接受)

                          公理 1          公理 2          公理 3
                        能力二分        不可约性        Pair 幂等
                       (Det/NonDet)    (无万能积木)    (P² ≅ P)
────────────────────────────────────────────────────────────────
接受的保障：
  确定性构件可验证       ✅              —               —
  Det 组合自动并行安全    ✅              —               —
  构件职责边界清晰        —              ✅              —
  构件数量自然收敛        —              ✅              —
  层级有理论上界          —              —               ✅
  部署前静态验证          ✅              ✅              ✅

拒绝的收益：
  快速原型灵活性         失去            获得             失去
  零配置智能节点          失去            获得             失去
  组织层级直译映射        失去            失去             获得
  运行时动态 Agent 创建   失去            失去             获得
  流程图自然直译          失去            —               失去
```

### 5.2 不同团队的选型指南

```
如果你的团队需要...              推荐公理组合       平台参考
──────────────────────────────────────────────────────────
企业合规场景（每步可审计）         全部三条           Lilies
  "必须能证明每个决策路径是可追溯的"
  "不能接受运行时动态创建未审计的 Agent"

研究探索场景（最大化灵活性）       全部拒绝           AutoGen
  "我想实验各种 Agent 交互模式"
  "暂时不需要生产级可靠性"

快速业务原型（非技术用户）         拒绝公理 2         Dify
  "业务人员需要自己搭流程"
  "不需要理解 Harness+LLM 的拆分"

复杂状态机编排                      拒绝公理 1 + 3    LangGraph
  "业务流程有复杂的循环和回溯"
  "LLM 需要驱动状态转移"

编码助手（结构化工具使用）         接受全部三条       Claude Code
  "工具调用需要确定性保证"
  "沙箱和权限是硬约束"
```

### 5.3 选型的诚实代价表

| 公理选择 | 你得到的 | 你失去的 | 适合你的信号 |
|---------|---------|---------|------------|
| **接受 1+2+3** | 部署前可验证、层级有界、构件职责清晰 | 快速原型灵活性、组织层级直译 | "我的系统需要运行在生产环境中，宕机有后果" |
| **拒绝 1** | 灵活的状态图、自然的流程图表达 | 静态验证、Det/NonDet 自动分析 | "我的业务流程本身就是复杂和非结构化的" |
| **拒绝 2** | 低门槛、零配置智能节点 | 构件爆炸、职责重叠、组合推理不可行 | "我的用户不懂编程，需要最简体验" |
| **拒绝 3** | 组织层级直译、动态子 Agent 创建 | 无层级上界、调试复杂度不可控 | "我的问题本质上是层级化的，且层级本身是动态变化的" |

---

## 第六章：Lilies 的结构性限制及其替代模式

本章汇总 Lilies 在三条公理下的三个结构性"不可达"，以及对应的替代设计模式。

### 6.1 ❌ 无限深度 Agent 嵌套 → 展平为并行子团队

```
不可达：
  Agent_A 创建 Agent_B，Agent_B 创建 Agent_C，Agent_C 创建 Agent_D...

展平替代：
  Agent_A
    ├── cluster_publish → SubTeam_1 (多 Agent 协作，通过 iteration 并行)
    ├── cluster_publish → SubTeam_2 (多 Agent 协作，通过 iteration 并行)
    └── cluster_publish → SubTeam_3 (多 Agent 协作，通过 iteration 并行)
  → cluster_subscribe 汇总

原理：
  深度嵌套 → 广度并行 的等价变换
  信息论依据：深度嵌套中的信息衰减使得第 4+ 层的贡献 < 噪声水平
  工程依据：level_3 = level_2 (不动点定理)
```

### 6.2 ❌ 运行时动态修改图拓扑 → 预置路径 + Builder 层元编程

```
不可达：
  工作流执行中 LLM 决定插入一个新节点

替代 A（预置路径）：
  在编译时将"所有已知的可能步骤"声明为 DAG 中的条件分支
  LLM 在运行时选择走哪条路径
  → 覆盖"已知的未知"

替代 B（Builder 层元编程）：
  如果确实遇到了"未知的未知"——
  LLM 判断当前 BlockFlow 不够用
  → 暂停执行
  → Builder Team 根据新需求生成新的 BlockFlow
  → 人工/自动审批
  → 新 BlockFlow 替换旧 BlockFlow
  → 恢复执行
  
  这比运行时动态插入慢，但每一步可审计、可测试

原理：
  编译时 vs. 运行时的分离是刻意设计的
  编译时（Builder 层）→ 有测试门禁和人工审批
  运行时 → 只有预声明的执行路径
```

### 6.3 ⚠️ 多 Agent 自由对话的便利性 → 封装为 Template

```
AutoGen GroupChat 的一行代码：
  groupchat = GroupChat(agents=[A, B, C], max_rounds=10)

Lilies 的等价 Template（展开后约 12-15 个积木）：
  MultiAgentDiscussionTemplate {
    start,
    loop(max_iterations=10) {
      iteration(participants, parallelism=N) {
        llm("角色扮演 + 阅读历史 + 表达观点"),
        cluster_publish("discussion.round.{n}", {role, opinion})
      },
      cluster_subscribe("discussion.round.{n}"),
      llm("共识检测"),
      if_else(consensus → break, no_consensus → continue)
    },
    llm("最终综合"),
    end
  }

一次封装后：
  Builder.expand_template("multi_agent_discussion", {
    participants: ["legal", "compliance", "finance"],
    topic: "合同审查",
    max_rounds: 5
  })
  → 等价于 AutoGen 的 GroupChat([Legal, Compliance, Finance], max_rounds=5)

成本：
  首次封装成本（一次性）：12-15 个积木的设计 + 测试
  后续使用成本：与 AutoGen 的 GroupChat 一行代码相同
```

---

## 第七章：工程验证——L1 集群实验的证据

### 7.1 实验配置

来自 [plan_categorical_theory_driven_engineering_v1.md](../../workingon/plan_categorical_theory_driven_engineering_v1.md) 的 Task Market 多 Agent 集群实验：

| 参数 | 值 |
|------|-----|
| 积木集 | L1（publish, subscribe, acquire, release） |
| 组件 | MessageBus + ConflictDetector |
| Agent 数 | 12（4 Producer + 8 Worker） |
| 交互轮数 | 120 |
| 消息/锁事件 | 1549 |

### 7.2 实验对公理选择的验证

**publish ⊣ subscribe 均衡性**：8 个 Worker 的认领分布均匀（7-8/worker），标准差 < 0.5。无中心调度器——伴随结构的自然属性保证了负载均衡。

**acquire/release 必要性**：单个 `db.results` 资源在 8 个 Worker 间产生 92.8% 的锁冲突率。这证明了 L1 的最小性——没有 acquire，并发写入会产生数据竞争。

**Det 闭包的并发安全**：publish/subscribe/acquire/release 都是确定性 Harness 操作——lock 表冲突、cursor 前进、idempotency 去重全部在 SQLite 层面由确定性规则保证。0 死锁。这验证了 Det 闭包定理：确定性原语在并发组合下保持可判定性。

**不动点的 empirical 验证**：120 轮、1549 个事件中未出现需要 level_3+ 结构的新模式。所有涌现行为（hot_resource, starvation, ping_pong）都在 level_2（cluster）内可表达。

---

## 第八章：结论

### 8.1 公理不是教条

本文的分析拒绝将 Lilies 的三条公理呈现为"唯一正确选择"。公理是保障的显式声明。接受一条公理意味着在某个维度上限制系统的灵活性以换取可验证性。拒绝一条公理意味着牺牲可验证性以换取灵活性。

### 8.2 公理是清醒的工程选择

这套框架的价值不在于证明"Lilies 是对的"，而在于使公理选择成为**显式的**：

> 每个 Agent 平台的设计者都做了公理选择。区别在于：大多数平台的选择是隐式的——公理隐藏在"技术栈选择"或"框架设计哲学"中，无法被明确表述和审视。Lilies 的选择是显式的——三条公理写在范畴论文档的前三页，每一条都有数学陈述、工程转译、收益分析和代价评估。

### 8.3 跨平台比较的终局

将六条平台放入公理框架后，一个清晰的模式浮现：

| 平台 | 公理选择 | 核心竞争力 |
|------|---------|----------|
| **Lilies** | 1+2+3 全部接受 | 可验证性、可测试性、层级有界 |
| **Claude Code** | 1+2+3 全部接受 | 结构化工具使用、权限模型、沙箱 |
| **OpenAI Agents SDK** | 1+2 接受，3 模糊 | API 生态、模型集成深度 |
| **LangGraph** | 1+2 模糊，3 拒绝 | 复杂状态编排、LLM 驱动控制流 |
| **Dify** | 1 模糊，2 拒绝，3 接受 | 低门槛、业务人员可用 |
| **AutoGen** | 1+2 模糊，3 拒绝 | 多 Agent 对话研究、动态交互 |

这不是一个"谁更好"的排名表。这是一个**保障选择表**——每个平台选了自己愿意保证的东西和愿意放弃的东西。Lilies 的独特性不在于"技术更先进"，而在于**公理选择是完全显式的**，以及由此推导出的**结构保证是可证明的**。

---

## 参考文献

1. Li & Jiang (2026). The Pair 形式系统：范畴论表述. Lilies 项目.
2. Li & Jiang (2026). The Pair 形式系统：从原子到集群的完整证明. Lilies 项目.
3. Li & Jiang (2026). Cluster Minimality Proof — Why 4 Blocks + 2 Components Is the Strict Lower Bound. Lilies 项目.
4. Li & Jiang (2026). Lilies 系统理论审视. Lilies 项目.
5. Moggi, E. (1991). Notions of Computation and Monads. *Information and Computation*, 93(1), 55–92.
6. Hasegawa, M. (1997). Recursion from Cyclic Sharing. *TLCA '97*, Springer LNCS 1210.
7. Mac Lane, S. (1971). *Categories for the Working Mathematician*. Springer.
