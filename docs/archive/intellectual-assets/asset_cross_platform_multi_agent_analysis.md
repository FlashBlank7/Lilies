# 多 Agent 协同的跨平台范畴论分析

**蒋之骏** · 2026年7月

**前置**：[[asset_the_pair_formal_system]] [[asset_cluster_minimality_proof]] [[asset_the_pair_categorical]]

**关联**：[[asset_harness_llm_composite]] [[asset_theoretical_review]]

---

## 摘要

本文以 The Pair 范畴论形式化的三条公理为统一语言，分析六大主流 Agent 平台（Lilies、Claude Agent SDK、OpenAI Agents SDK、LangGraph、CrewAI、AutoGen/Microsoft Agent Framework）在多 Agent 协同上的架构选择。核心发现：

1. **所有平台的差异可归约到三个范畴选择**：Harness 的具象化位置、Agent 间交互拓扑、对公理 3（Pair 幂等）的态度。
2. **CrewAI 是结构上最接近 Lilies 的平台**——其 Flow + Crew 双模架构是 The Pair 范式在另一个框架中的独立发现。
3. **公理 3 的选择是最强的结构分叉点**——满足 $P^2 \cong P$ 的平台（Lilies、CrewAI、Claude Agent SDK）提供了层级有界性保证；违反它的平台（LangGraph、AutoGen）提供了更大的灵活性，但层级数无理论上界。
4. **Lilies 是唯一同时满足全部三条公理并提供 Lamport 时钟因果序、Det 闭包并发安全判据、以及涌现模式自动检测的平台**。

---

## 第一章：分析方法

### 1.1 统一分析框架

本文使用以下统一框架分析每个平台的多 Agent 协同设计：

```
分析维度 1：Harness 在哪里？
  → 确定性骨架在架构中的具象化位置
  → 分离程度（强制 vs 可选，编译时 vs 运行时）

分析维度 2：Agent 间交互拓扑是什么？
  → 中心化调度 vs 去中心化通信
  → 拓扑的可编程性（固定 vs 可配置 vs 完全可编程）

分析维度 3：对递归嵌套的态度（公理 3 的选择）
  → P² ≅ P vs P² ≠ P
  → 层级数有理论上界 vs 无理论上界
  → 有界性来自理论保证 vs 工程限制 vs 完全没有

分析维度 4：并发安全的结构保证
  → 锁机制 vs 无锁
  → 乐观并发 vs 悲观并发
  → 安全判据的形式化程度
```

### 1.2 公理回顾

来自 [[asset_the_pair_categorical]]（`the_pair_categorical.tex`）：

**公理 1（能力二分）**：$\catCap$ 的每个态射恰属于 $\catDet$ 或 $\catNonDet$ 之一。不存在其他类型的态射。

**公理 2（不可约性）**：不存在非平凡的满子范畴 $\cat{X} \subset \catCap$ 使得 $\cat{X} \neq \catCap$ 且 $\cat{X} \neq \catDet$ 且 $\cat{X} \neq \catNonDet$，并且 $\cat{X}$ 包含所有实用能力。

**公理 3（Pair 幂等）**：$\pairMonad^2 \cong \pairMonad$。将 Pair 分解再次应用于自身，不会发现新的分解。

**公理 3 的推论（不动点定理，Thm 7.1）**：$\pairMonad(\mathrm{level}_2) = \mathrm{level}_2$。层级谱系在集群层饱和。不存在 level_3+。

---

## 第二章：六大平台的逐项分析

### 2.1 Lilies

**协同模型**：DAG 工作流 + Topic pub/sub + 分布式锁

**Harness 的具象化位置**（三层全部）：
```
level_0（积木层）: JSON Schema + 端口类型 + 配置验证
level_1（工作流层）: WorkflowSpec DAG + validate_workflow + test gate
level_2（集群层）: ClusterMessageBus + ConflictDetector + ClusterRegistry
```

**Agent 间交互拓扑**：去中心化。Topic-based pub/sub（伴随对 publish ⊣ subscribe）定义通信通道；acquire/release 定义并发安全边界。Fan-Out、Fan-In、Pipeline 均由基本通信原语的组合实现。

**公理 3 的态度**：**理论严格满足**。$P^2 \cong P$ 是不动点定理（Thm 7.1）的前提；公理 3 独立性定理（Thm 8.5）提供了显式的拒绝递归 Agent 嵌套的形式理由。层级数 ≤ 3（积木 → 工作流 → 集群），由不动点定理保证。

**并发安全的结构保证**：
- Det 闭包定理（Thm 8.3）：确定性操作在 $\circ$、$\otimes$、$\mathrm{Tr}$ 下闭合。Det-only 路径可安全并行，无需锁。
- L1 完全性定理（Thm 8.4）：$\{\mathrm{publish}, \mathrm{subscribe}, \mathrm{acquire}, \mathrm{release}\}$ 是实现并发安全的完全集。所有并发协议可归约到 4 个积木。
- Lamport 时钟：因果序可判定。确定性调度模式下交互可精确重放。

**独特能力**：
- 涌现模式的自动检测（ClusterAnalyzer：4 类消息流模式 + 4 类锁竞争模式 + 4 类涌现信号）
- 定理的实证验证（5 项自动检查：L1 完备性、Det 闭包、不动点、伴随性、无死锁）
- 传输层可替换（MessageTransport protocol：SQLite → Redis → Kafka，函子抽象保证语义不变）

---

### 2.2 CrewAI — 结构上最接近 Lilies 的平台

**协同模型**：Flow（确定性骨架）+ Crew（角色扮演的协作智能）

**Harness 的具象化位置**（双模分离——六大平台中最清晰的 Harness/LLM 分离）：
```
Flow 层（≡ Harness）:
  - @start / @listen / @router 装饰器定义事件驱动的确定性骨架
  - 状态管理、条件逻辑、循环、分支
  - 2026 年生产建议："先用 Flow 包装，再嵌套 Crew"

Crew 层（≡ LLM 智能）:
  - 角色扮演的自主 Agent（role, goal, backstory）
  - 支持三种进程：Sequential / Hierarchical / Consensus
  - 输出可以是 Pydantic schema（结构化，可测试）
```

**Agent 间交互拓扑**：半中心化。Process 类型定义了任务流拓扑——Sequential 是线性流水线，Hierarchical 是 Manager → Worker 树形结构。Agent 之间的交互由 Manager（中心调度器）或任务顺序（隐式拓扑）决定。

**公理 3 的态度**：**实践接近满足**。Flow(Crew(Flow(...))) 的嵌套不被推荐。Crew 本身不支持递归嵌套——Crew 是执行单元，不创建新的 Crew。但 Flow 可以调用多个 Crew，每个 Crew 独立执行。

**与 Lilies 的结构对应**：
| CrewAI 概念 | Lilies 对应 | 差异 |
|------------|-----------|------|
| Flow | WorkflowSpec DAG | Lilies 的 DAG 在编译时验证；CrewAI 的 Flow 是运行时解释的 |
| Crew | Builder Team 策略 + WorkflowRuntime 执行 | CrewAI 的 Agent 自主性更高（可以拒绝任务） |
| Hierarchical Process | cluster_publish/subscribe（Fan-Out） | CrewAI 的 Manager 是中心化调度器；Lilies 的 Worker 自主拉取 |
| Sequential Process | 顺序复合的工作流 DAG | 本质相同 |
| Task.output_pydantic | NodeContract（运行时 I/O 验证） | Lilies 的契约验证更严格 |
| @persist() | checkpoint_resume | 两者都是状态持久化 |

**Lilies 可借鉴的**：
1. Flow 与 Crew 的显式命名分离——"骨架"和"内容"在 API 层面就不同。Lilies 可以更显式地区分 "Workflow DAG = Harness 骨架" 和 "Builder/LLM 决策 = 智能内容"。
2. Task-level tool scoping（任务的工具子集覆盖 Agent 的工具集）——最小权限的动态实施。对应 Lilies 中 Sandbox Boundary 的细粒度控制。

---

### 2.3 Claude Agent SDK — 公理 3 最接近 Lilies 的平台

**协同模型**：Supervisor → Task tool → Subagent（上下文隔离的分发模式）

**Harness 的具象化位置**：
```
Runner 层（stateless）:
  - 管理 Agent 生命周期
  - 工具调用的权限和沙箱
  - maxTurns 限制防止失控

Task tool 层（dispatch 边界）:
  - 严格的上下文隔离：subagent 不继承父对话历史
  - 工具子集限制：每个 subagent 只有被显式授予的工具
  - 仅返回最终结果（中间工具调用不可见）
```

**Agent 间交互拓扑**：中心化分发。Supervisor 通过 `Task` tool 将工作分发给 subagent。每个 subagent 在独立上下文中运行，返回单一结果字符串。支持并发分发（parallel fan-out with subagents）。

**公理 3 的态度**：**工程上有界**。Subagent 可以嵌套 subagent（最多 5 层）。这不是理论保证——这是工程限制（防止上下文爆炸和 token 成本失控）。官方推荐"扁平化嵌套"（flatten to single supervisor with leaf-agent tools），这与 $P^2 \cong P$ 的实践方向一致。

**与 Lilies 的结构对应**：
| Claude Agent SDK 概念 | Lilies 对应 |
|----------------------|-----------|
| Task tool | cluster_publish（分发任务到 topic） |
| Subagent isolated context | 每个 Worker 独立订阅 topic，cursor 隔离 |
| maxTurns | LoopConfig.max_iterations |
| AgentDefinition.tools | BlockDefinition（每个积木有确定的输入/输出端口） |
| Background execution | asyncio.create_task（并发 subagent） |
| Context isolation | Worker 不共享 cursor，消息独立消费 |

**Lilies 可借鉴的**：
1. **Context isolation 是 subagent dispatch 的核心设计**。Lilies 的 `peek_messages` 已经是非消费的观察——可以进一步增强为"上下文隔离的子工作流"。
2. **"The dispatch is the unit" 评估模式**——不仅评估最终结果，还评估 dispatcher 的选择是否正确、subagent 是否在其 scope 内运行、supervisor 是否真正使用了 subagent 的返回值。这是 Lilies 遥测层可以增加的评估维度。

---

### 2.4 OpenAI Agents SDK — 最小化设计，最大化 LLM 自主权

**协同模型**：Handoff 作为工具调用——"切换到另一个 Agent"被建模为 LLM 调用一个特殊的 transfer 工具

**Harness 的具象化位置**（极简——核心 runner 约 800 行）：
```
Runner（stateless loop）:
  while True:
    if model_output == plain_text    → NextStepFinalOutput（停止）
    if model_output == handoff_tool  → NextStepHandoff（切换 Agent）
    if model_output == regular_tool  → NextStepRunAgain（执行工具后继续循环）

Guardrail（可选）:
  - 输入/输出验证器
  - 可以与模型调用并行运行
  - 不是架构强制的——是可选的附加层
```

**Agent 间交互拓扑**：中心化，LLM 决策驱动。Handoff 完全由 LLM 决定——何时换、换到谁、传什么参数。Runner 只是执行 LLM 的决策——它不做结构性干预。

**公理 3 的态度**：**无界**。Handoff 链可以任意长（Agent A → Agent B → Agent C → ...）。唯一的阻止是 `max_turns=10`（默认工程限制）。没有理论饱和点。

**与 Lilies 的对比**：
| 维度 | OpenAI Agents SDK | Lilies |
|------|------------------|--------|
| 并发安全 | ❌ 无锁机制。多 Agent 并发安全由调用者自己保证 | ✅ acquire/release + ConflictDetector |
| 消息持久化 | ❌ 无消息队列。handoff 传递上下文在内存中 | ✅ SQLite WAL 持久化 |
| 因果序 | ❌ 无 Lamport 时钟 | ✅ 每条交互可追踪因果序 |
| 涌现分析 | ❌ 无遥测层 | ✅ 自动模式检测 |
| 可复现性 | ❌ 每次运行可能不同 | ✅ 确定性调度模式 |
| 代码量 | ~800 行（Runner） | 3947 行（完整集群基础设施 + 遥测 + 分析） |

**评价**：OpenAI Agents SDK 选择了**最小化设计**——它为 LLM 提供了最大的自主权（所有决策由 LLM 做出），但提供了最少的结构保证。它的 800 行 Runner 证明"最小化是正确的"——Lilies 的 576 行 `cluster_runner.py` 也保持了极简。

---

### 2.5 LangGraph — 最灵活，但也最缺乏结构保证

**协同模型**：StateGraph + 子图/工具调用。任意状态机——图节点可以是 LLM 调用、工具执行、子图评估。

**2026 年三种并存的多 Agent 模式**：

```
模式 1（推荐）：Subagent-as-tool
  create_agent() → 包装为 @tool → 主 Agent 通过 tool-calling 路由
  结构：扁平。所有 subagent 都是 tool。

模式 2（可选）：Handoff/Swarm（langgraph-swarm）
  create_handoff_tool() → Command(goto=agent_name)
  结构：Agent 之间可以直接切换。

模式 3（已弃用但仍在广泛使用）：Supervisor graph
  create_supervisor(agents=[...])
  结构：显式层级。Supervisor → Worker → Sub-Worker。
```

**Harness 的具象化位置**：StateGraph 的结构定义——类型化状态对象、边定义、条件分支、checkpoint 持久化。这比 OpenAI 的 stateless runner 提供了更强的 Harness，但比 CrewAI 的显式 Flow/Crew 分离更松散。

**Agent 间交互拓扑**：完全可编程。开发者定义图的边，即定义 Agent 之间的交互路径。Graph 可以是有环的（Agent A → B → A）——没有结构性约束阻止任意拓扑。

**公理 3 的态度**：**无界**。三种模式都不阻止递归嵌套。`create_agent` 可以调用另一个 `create_agent`（作为 tool 或子图节点）。LangGraph 本身不强加层级限制——开发者自行决定何时停止嵌套。

**范畴分析**：
| 模式 | 公理 3 行为 | 结构保证 |
|------|-----------|---------|
| Subagent-as-tool | **近似 $P^2 \cong P$** | 扁平结构，tool 调用的边界清晰 |
| Handoff/Swarm | **$P^2 \neq P$** | Agent A → B → A 可以无限循环。无自然停止条件 |
| Supervisor graph | **$P^2 \neq P$** | 显式多层嵌套。层级数由开发者决定 |

**评价**：LangGraph 不选择公理 3——它把选择权交给开发者。这既赋予最大灵活性（可以构建任意拓扑的 Agent 系统），也放弃了一切结构保证（无法证明系统会饱和、无法保证无死锁、无法判定层级上界）。

---

### 2.6 AutoGen → Microsoft Agent Framework — 从对话到工作流的演化

**协同模型（AutoGen v0.4）**：GroupChat + GroupChatManager — 中心化管理器在多 Agent 之间分配"对话轮次"

**协同模型（Microsoft Agent Framework，2026）**：合并 AutoGen + Semantic Kernel。新增持久会话、checkpoint、OpenTelemetry 观测、图形化 DAG 工作流。

**Harness 的具象化位置**：
```
AutoGen v0.4:
  GroupChatManager 的调度规则（speaker_selection, max_round, termination）
  → 对话调度是确定性规则，但调度决策可以由 LLM 辅助（auto speaker selection）

MAF（2026 新增）:
  DAG 工作流（graph-based deterministic processes）
  → 这是对"确定性骨架缺失"的补课
  OpenTelemetry 观测
  → 这是对"可观测性缺失"的补课
```

**Agent 间交互拓扑**：中心化（GroupChatManager 决定发言顺序）+ 异步消息（v0.4 的 RoutedAgent publish/subscribe 模式）。支持嵌套 GroupChat（"团队中的团队"）——层级式组织建模。

**公理 3 的态度**：**无界**。嵌套 GroupChat 显式支持多层嵌套——GroupChat 内部可以包含另一个 GroupChat。AutoGen 文献将此称为"层级式组织建模"——每个子团队有自己的 Manager。

**MAF 演化的理论意义**：

AutoGen → MAF 的演化路径非常重要——它验证了 The Pair 范式的一个核心预测：

> 仅靠 LLM 对话无法保证生产可靠性。任何成熟的 Agent 平台最终都会被迫引入确定性骨架（DAG 工作流、checkpoint、观测）——即使这些概念在其初始设计中并不存在。

MAF 新增的 DAG 工作流 + OpenTelemetry + 持久会话 = **补课式地添加 Harness 层**。这正是 Lilies 从一开始就内置的设计——Harness 不是事后附加的"治理层"，而是与 LLM 同等地位的**架构原语**。

---

## 第三章：三个范畴选择的比较矩阵

### 3.1 Harness 在哪里？

```
平台              确定性骨架的具象化                   分离程度
────────────────────────────────────────────────────────────────
Lilies           每层独立 Harness（Schema/DAG/Bus+Lock） 强制，架构级
CrewAI           Flow 层（显式骨架）＋ Crew 层           强制，API 级
Claude Agent SDK Runner + Task tool + maxTurns           强制，框架级
OpenAI SDK       Runner + 可选 Guardrail                 弱，可选附加
LangGraph        StateGraph 结构 + checkpoint             半强制，开发者定义
AutoGen → MAF    Manager 规则 → DAG 工作流               从弱（对话规则）到强（DAG）
```

**规律**：更成熟的平台倾向于更显式、更强制、更结构化的 Harness 分离。新平台（OpenAI Agents SDK）倾向于最小化 Harness，将更多决策权交给 LLM。

### 3.2 Agent 间交互拓扑是什么？

```
平台              交互模型                    拓扑结构
─────────────────────────────────────────────────────────────
Lilies           去中心化 topic pub/sub       任意 DAG（publish/subscribe 组合）
CrewAI           半中心化 Process             树形 / 线性
Claude Agent SDK 中心化 Task 分发             树形（Supervisor → subagents）
OpenAI SDK       中心化 Handoff               线性链 / 树形
LangGraph        完全可编程 StateGraph          任意有向图（包含环）
AutoGen → MAF    中心化对话 + 去中心化消息      嵌套 GroupChat
```

**规律**：中心化调度（Claude、OpenAI、CrewAI Manager）简化了实现但成为单点瓶颈。去中心化（Lilies pub/sub）或完全可编程（LangGraph）提供了更大的灵活性，但需要更强的并发安全保证。

### 3.3 公理 3 的选择

```
平台              P² ≅ P？     层级上界          上界的性质
────────────────────────────────────────────────────────────────
Lilies           ✅ 严格        ≤ 3（不动点定理） 理论保证
CrewAI           ✅ 实践        ≤ 2（Flow+Crew）  架构惯例
Claude Agent SDK ⚠️ 近似        ≤ 5（max subagent 理论：有界但非饱和；实践：推荐扁平化
                              nesting depth）
OpenAI SDK       ❌ 无         无理论上界         ─
LangGraph        ❌ 无         无理论上界         ─
AutoGen → MAF    ❌ 无         无理论上界         ─
```

**规律**：公理 3 的选择是**最强烈的结构分叉点**。满足 $P^2 \cong P$ 的平台共享一个核心特征：层级饱和——系统在某个层级后不产生新的结构。违反 $P^2 \cong P$ 的平台共享相反的特征：开发者可以无限制地添加层级，但随之而来的是调试难度、协调开销和不可预测性的无界增长。

---

## 第四章：Lilies 的独特位置

### 4.1 结构保证矩阵

在六大平台的比较中，Lilies 是唯一提供以下**全部**结构保证的平台：

| 结构保证 | Lilies | CrewAI | Claude | OpenAI | LangGraph | AutoGen |
|---------|:---:|:---:|:---:|:---:|:---:|:---:|
| 层级有理论上界 | ✅ | ✅ | ⚠️ (5) | ❌ | ❌ | ❌ |
| 并发安全形式判据 | ✅ Det 闭包 | ❌ | ❌ | ❌ | ❌ | ❌ |
| 通信原语最小性证明 | ✅ L1 完全性 | ❌ | ❌ | ❌ | ❌ | ❌ |
| Lamport 时钟因果序 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 确定性可复现 | ✅ | ⚠️ Flow | ❌ | ❌ | ⚠️ Graph | ❌ |
| 传输层可替换 | ✅ Protocol | ❌ | ❌ | ⚠️ | ⚠️ | ❌ |
| 涌现模式自动检测 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 定理实证验证 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |

### 4.2 Lilies 不是"功能最多"的平台，而是"结构保证最严格"的平台

这个定位是刻意的——来自 The Pair 范畴论形式化的三条公理：

1. **公理 1（能力二分）** → 强制每个组件可判定地分类为确定/非确定 → 乐观/悲观并发策略有精确的理论判据。
2. **公理 2（不可约性）** → 强制积木集在 CORE 的生成闭包中 → 新积木必须是真正的新生成元，不是已有积木的隐式组合。
3. **公理 3（Pair 幂等）** → 强制层级饱和 → 不会出现"在 level_3 上发现了一个 bug，修好后又出现了 level_4"。

这三条公理共同产生了 Lilies 的核心价值主张：**不是"能做什么"（其他平台也能），而是"保证不会发生什么"（其他平台无法保证）。**

具体地：

| Lilies 保证不会发生 | 其他平台 |
|-------------------|---------|
| 层级无限增长（level_4, level_5, ...） | LangGraph、AutoGen：发生；Claude：有界但不保证 |
| 递归 Agent 嵌套导致的协调开销爆炸 | LangGraph、AutoGen：发生 |
| 未加锁的并发写入导致数据竞争 | OpenAI、LangGraph：发生 |
| 无法从遥测复现的错误交互 | 所有平台：无 Lamport 时钟 |
| 新积木的无限增殖（违反公理 2） | Dify 式"智能节点"膨胀 |

---

## 第五章：Lilies 可从其他平台借鉴的

### 5.1 从 CrewAI

1. **Flow/Crew 的显式命名分离**。"骨架"和"内容"在 API 层面就不同。Lilies 可以更显式地使用术语：`WorkflowSpec` = Harness 骨架，`Builder strategy` / `Worker decision` = LLM 智能内容。

2. **Task-level tool scoping**。任务的工具子集动态覆盖 Agent 的工具集——"最小权限"的运行时实施。对应 Lilies 中 Sandbox Boundary 的更细粒度控制。

### 5.2 从 Claude Agent SDK

1. **Context isolation 是 subagent dispatch 的核心**。Lilies 的 `peek_messages`（非消费观察）可以增强为更严格的"上下文隔离的子工作流"——类似 Claude 的 `Task` tool 语义。

2. **"The dispatch is the unit" 评估模式**。当前 Lilies 的遥测层评估"最终结果"。可以增加三个独立评估维度：dispatch 正确性（Worker 是否选择了正确的任务？）、scope 保真度（Worker 是否在其能力范围内执行？）、result 集成度（Producer 是否使用了 Worker 的结果？）。

### 5.3 从 OpenAI Agents SDK

1. **极简 Runner**。800 行的 Runner 证明"最小化是正确的"。Lilies 的 576 行 `cluster_runner.py` 保持了这个方向。进一步简化的空间有限，但值得持续审视。

2. **Guardrail 的并行执行**。OpenAI 的 guardrail 可以与 LLM 调用并行运行（不增加延迟）。Lilies 的遥测检测也可以异步化——在 Worker 执行的同时记录遥测，而非事后分析。

### 5.4 从 LangGraph

1. **`Command(goto=...)` 的显式状态转移**。在 Lilies 中可以对应 topic 之间的路由（`if_else` → `cluster_publish` 到不同 topic）。不需要新的积木，但可以做成预置 Template（如 `router_pattern`）。

2. **Interrupt propagation**。`interrupt()` 在嵌套 subagent 中向上冒泡。对应 Lilies 中 `cancel_condition` 在嵌套 loop 中的传播。

### 5.5 从 AutoGen → MAF 的演化

1. **演化路径的验证**。AutoGen 从纯对话式演化到对话+工作流混合——证明**仅靠 LLM 对话无法保证生产可靠性**。这验证了 Lilies 从一开始就内置确定性骨架的决策。

2. **MAF 的 DAG 工作流 + OpenTelemetry = Lilies 的 DAG + Lamport 遥测**。两者在功能上趋同，但 Lilies 的遥测更强（因果序 vs 时间序），MAF 的生态系统更大（Azure 集成）。

---

## 第六章：结论

### 6.1 核心发现

1. **所有六大平台的多 Agent 协同设计差异可归约到三个范畴选择**：Harness 的位置、交互拓扑、公理 3 的态度。不存在"完全不同的范式"——只有同一套范畴结构的不同实例化。

2. **CrewAI 是 Lilies 在结构上最接近的同类**。其 Flow + Crew 双模架构是对 The Pair 范式的独立发现——两者在不知道对方存在的情况下达到了高度相似的结构分离。

3. **公理 3（Pair 幂等）是最强的结构分叉点**。选择 $P^2 \cong P$ 的平台获得了层级有界性保证；选择 $P^2 \neq P$ 的平台获得了更大的灵活性，但失去了对系统复杂度的理论上界控制。

4. **Lilies 的独特位置**：不是"能做最多"的平台，而是"结构保证最严格"的平台——唯一同时满足全部三条公理、唯一提供 Lamport 时钟因果序、唯一提供涌现模式自动检测、唯一提供定理实证验证。

### 6.2 Lilies 的长期策略含义

```
短期（6 个月）：
  → 借鉴 CrewAI 的 Flow/Crew 术语显式化
  → 借鉴 Claude Agent SDK 的 dispatch 评估模型
  → 保持极简 Runner（参考 OpenAI 800 行）

中期（12 个月）：
  → 传输层协议化（SQLite → Redis → Kafka，函子抽象）
  → LLM Worker 自适应策略（公理 3 保证的新模式仍在 L1 范围内）
  → Context isolation 增强

长期（24 个月）：
  → 跨平台验证：证明另五个平台的多 Agent 模式均可归约到 L1 的 {P,S,A,R} 组合
  → Lilies 的保证在任意满足公理 1-3 的平台上成立——不限于当前实现
```

---

## 参考文献

1. `docs/source-materials/2026-07_initial_architecture_research/the_pair_categorical.tex` — The Pair 范畴论形式化（20 个定理）
2. `docs/intellectual-assets/asset_the_pair_formal_system.md` — The Pair 形式系统（FS 版本）
3. `docs/intellectual-assets/asset_cluster_minimality_proof.md` — L0/L1/L2 最小谱系证明
4. `docs/intellectual-assets/asset_harness_llm_composite.md` — Harness+LLM 原子性资产
5. LangGraph 文档：`docs.langchain.com` — Supervisor deprecation, subagent-as-tool pattern (2026)
6. AutoGen v0.4 架构：`microsoft.github.io/autogen` — 三层架构 (core/agentchat/ext)
7. OpenAI Agents SDK：`github.com/openai/openai-agents-python` — Handoff as tool call, Runner loop (2026)
8. CrewAI 文档：`docs.crewai.com` — Flow + Crew dual-layer architecture (2026)
9. Claude Agent SDK：`code.claude.com/docs/en/agent-sdk` — Subagent dispatch, Task tool, context isolation (2026)

---

*Every platform's multi-agent design is a choice of where to place Harness, what topology to allow, and whether to accept P² ≅ P. Lilies chose: every layer, any DAG, and yes.*
