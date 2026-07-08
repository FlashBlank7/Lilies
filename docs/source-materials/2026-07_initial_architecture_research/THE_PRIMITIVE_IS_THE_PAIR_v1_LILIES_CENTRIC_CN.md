# 原语即耦合：Harness+LLM 作为智能体架构的原子单元

> **版本说明**: v1 中文版 (2026-06-26) — 以 Lilies 系统为中心的原始版本。本文以 Lilies 的建筑过程为主叙事线，用 Lilies 来论证范式的合理性。v2 (2026-06-29) 重构为以范式为中心。两者互补：v1 适合想深入理解 Lilies 设计与实现的读者，v2 适合想理解范式通用性的读者。

---

## 摘要

AI 智能体领域面临一个根本性的定义困境。"Agent"在不同框架中意味着完全不同的东西——一次 LLM 调用、一个多轮自主系统、一个内嵌推理的可视化节点。关于"什么是 Agent"的争论陷入了循环，因为它们依赖外延式定义。本文提出一个统一范式：**任何智能体系统的原子单元不是"Agent"，而是 Harness+LLM 复合体。** Harness 提供确定性的、可测试的、可组合的执行基础设施。LLM 提供非确定性的、自适应的语义推理。这个复合体不是"Agent 是什么"——它是所有被称作 Agent 的东西共同的结构不变量。我们以 Lilies 系统为验证载体，在该系统中智能体的能力从 41 个积木的拓扑关系中涌现。通过三层架构、带溯源追踪置信度模型的模板市场、以及从人-AI 协作中自动提取可复用工作流的元认知层，我们证明了 Harness+LLM 复合体不仅是一个描述工具，更是一个可操作的工程原则。实验结果包括多智能体代码生成 76/76 测试通过、模板展开比从零搭建快 2,292 倍、非 LLM 工作流五次运行输出完全一致。

---

## 1. 引言

### 1.1 定义之困

什么是 Agent？问十个从业者，你会得到十一种答案。每种定义都是**外延式的**——它列举某个特定系统恰好具有的属性。每种定义在自己的系统内正确，但都无法泛化。

根因不是 Agent 系统太过多样而无法统一。根因是**"Agent 是什么"这个问题本身是错的。** Agent 不是原子事物。它们是复合事物，同时在多个粒度上出现。

### 1.2 结构不变量

考察三个都被称为"Agent"的系统：

1. **Lilies 中单个 `model_turn` 积木**：JSON Schema 配置包裹 DeepSeek API 调用。这是 Agent 吗？

2. **14 积木 Agent Loop**：`context_assembler → model_turn → tool_call_router → ... → event_recorder`。每个积木是一个复合体，整条链也是一个复合体。哪个是 Agent？

3. **Lilies 平台本身**：41 积木 + DAG 引擎 + Builder + Agent Factory + 模板市场 + 元认知层。整个平台是 Agent 吗？

答案是**三者都是 Agent——因为它们都是同一结构不变量的实例。** 每个都是确定性执行基础设施（Harness）与非确定性语义推理（LLM）的耦合。唯一的区别是**粒度**。

### 1.3 鸡与蛋洞见

```
尝试: 在 if_else 内部嵌入基于 LLM 的路由逻辑
结果: 微型 question_classifier + if_else 复合体被藏进更小的壳
模式: 复合体是同一个。只是容器不同。

尝试: 创建"万能Agent节点"自动选择策略
结果: 微型工作流编排循环藏进节点内部
模式: 复合体是同一个。只是壳更小。

尝试: 单次 prompt 完成多步推理（Chain-of-Thought）
结果: 一次 LLM 调用内部的微型推理循环
模式: 复合体是同一个。只是容器变成了 prompt。
```

**鸡（已有复合体）和蛋（被优化的原语）在结构上是同一个东西。** 每种创建"更聪明原语"的尝试，不过是在更细粒度上重建了同一个复合体。

### 1.4 本文贡献

1. **一个统一范式**（§2）：Harness+LLM 作为所有 Agent 系统的结构不变量。
2. **六个工程推论**（§3）：积木永远不需要变聪明、优化应投向组合层、LLM 是唯一整体提升因素等。
3. **一个完整参考实现**（§4）：Lilies，从原子积木到元认知层的全谱系系统。
4. **定量实验证据**（§5）：多 Agent 代码生成 76/76 通过、模板展开速度 ×2,292 倍等。

## 2. Harness+LLM 复合体

### 2.1 定义

```
能力 = Harness (硬) + LLM (软)

Harness: 确定、可测试、可组合、可验证
LLM:    非确定、自适应、语义化、非确定性
```

**Harness** 是可以用单元测试验证的部分。JSON Schema、DAG 拓扑、预算门——使行为**可预测**。

**LLM** 是无法用单元测试验证的部分。系统提示词、语义推理、创造性生成——使行为**具有智能**。

**两者构成不可再分的单元。** 仅有 Harness 是确定性函数，仅有 LLM 是文本生成器。任何 Agent-like 系统都同时包含两者。

### 2.2 粒度独立性

复合体在每个层次反复出现：

| 层次 | Harness | LLM | 示例 |
|------|---------|-----|------|
| 积木级 | JSON Schema + 端口 | model_turn 系统提示词 | question_classifier |
| 链级 | DAG 拓扑 | 积木内 LLM 调用 | 14-block Agent Loop |
| 模板级 | 固定积木序列 | Builder 语义理解 | code_reviewer |
| 系统级 | 41 积木 + DAG 引擎 | Builder + Agent Factory | Lilies |
| 元级 | DecisionTracker 结构 | Review Agent 分析 | 元认知层 |
| 平台级 | JWT、Docker cgroups | 无——纯硬约束 | Platform Harness |

**不存在"原子 Agent"。只存在当前层级的 pragma。**

### 2.3 跨框架对比

每个 Agent 框架都对复合体的放置位置做出了隐性选择：

| 框架 | 复合体位置 | 粒度 | 积木/节点角色 |
|------|----------|------|-------------|
| LangGraph | StateGraph 中 | 图 = Agent | 节点是纯函数或 LLM 调用 |
| Dify | 每个节点内部 | 节点 = Agent | 节点内部包裹 Harness+LLM |
| AutoGPT | 主循环中 | 循环 = Agent | 工具是单体循环的子过程 |
| OpenAI Agents SDK | Agent 配置中 | 配置 = Agent | 带指令和工具的配置化 LLM |
| Claude Code | 查询引擎中 | 会话 = Agent | 工具+权限+沙盒围绕单一模型 |
| **Lilies** | **DAG 拓扑中** | **拓扑中的位置 = Agent** | **纯 Harness 或纯 LLM；复合体在连接中** |

Lilies 的独特选择是将复合体放在**积木之间的连接关系**中，而非积木内部。

### 2.4 范式的有用性

三个预测：

**预测 1**: 改善 Harness 部分 → 所有共享该 Harness 的 Agent 同步提升。（已证：max_output_tokens 8192→4096，可靠性 ~60%→~85%）

**预测 2**: 改善 LLM 部分 → 所有使用该 LLM 的 Agent 同步提升，架构零变更。（已证：DeepSeek high→xhigh，Builder +35%，Prompt +73%，JSON 截断消除）

**预测 3**: 仅改变 LLM 不改变 Harness → 效果不可预测且受 LLM 能力约束。（已证：同类模型提示词工程 ±15% 波动 vs 模型升级 +73%）

## 3. 架构推论

### 3.1 积木永远不需要变聪明

积木的角色是提供确定的运行时机制。它的"智能"来自 DAG 中的位置，而非内部复杂度。

```
推论 1: 积木是乐高砖块（硬，形状固定）。
积木组合是建筑（软，无限可能）。
你不需要更聪明的砖块。
你需要更好的建筑图纸（模板）和更好的建筑师（Builder）。
```

### 3.2 所有优化应投向组合层

| 类别 | 示例 | 正确性 |
|------|------|--------|
| 积木级 | 在 if_else 中嵌入 LLM | ❌ 违反复合体 |
| LLM 级 | 更好提示词、更高效模型 | ✅ 同步提升所有 Agent |
| 组合级 | 模板质量、Builder 工程、元认知 | ✅ 放大平台核心竞争力 |

设计检视三问：在哪一层？软智能藏进硬壳？加速飞轮？

### 3.3 积木数量已触及上限

41 个积木覆盖 15 项 Harness 能力。新增积木仅当真正新的运行时机制出现。**不存在"更好的 if_else"，只存在"if_else 在更好的工作流中"。**

### 3.4 三条飞轮

**发现飞轮**: 需求 → 搜索模板 → 匹配 → 展开 → 复用

**提取飞轮**: 搭建成功 → 自动提取 → 门控 → 合并 → 置信度↑

**推荐飞轮**: 高置信度 → 排名靠前 → 更多采用 → 更高使用量 → 更高质量分

### 3.5 软硬分层是正确的架构

```
Layer 3 (软):    模板市场 + 元认知层 — 自然语言理解
Layer 2 (半软):  Builder + Agent Factory — LLM 推理
Layer 1 (硬):    41 积木 + DAG + Schema — 确定性执行
Platform:         JWT + Docker + 配额 — 硬约束
```

跨层优化是架构债务的主要来源。

### 3.6 更好的 LLM 是唯一整体提升因素

更强的 LLM → Builder 更精确 → Agent Factory 更可靠 → 元认知更准确 → 模板置信度更快增长 → 所有飞轮加速。**积木本身永远不变。** 工作流是模型无关的资产。

## 4. Lilies 系统

### 4.1 架构总览

Lilies 是一个工作流平台，Agent 能力从 DAG 拓扑中涌现。它在三层架构（§3.5）内以六种粒度（§2.2）实例化复合体。

核心设计原则：**基于 LLM 的决策表现为积木之间的连接，而非积木内部的逻辑。**

### 4.2 41 积木系统

**业务工作流积木（16）**: start, schedule_trigger, llm, claude_agent (旧版), tool, if_else, question_classifier, parameter_extractor, template_transform, variable_assigner, variable_aggregator, http_request, iteration, loop, human_input, end, answer

**Agent 架构积木（25）**: context_assembler, workspace_context_injector, conversation_memory, context_compactor, model_turn, tool_call_router, stop_continue_controller, retry_error_classifier, tool_executor, tool_result_normalizer, permission_gate, sandbox_boundary, skill_loader, mcp_gateway, capability_registry, subagent_spawn, task_dispatcher, mailbox_wait_wake, dependency_gate, budget_gate, round_limit, cancellation_point, checkpoint_resume, event_recorder, hook_point

每个 Agent 架构积木对应 Agent 系统中可观察到的具体运行时机制。25 个积木覆盖 15 项 Harness 能力，全部为原创 Python 实现。

### 4.3 模板市场

模板是"工作流作为可复用资产"的编码。

**设计**: JSON 文件存储、Fork 模型、发布回环。

**内置模板（9 个）**: code_reviewer、data_analyzer、customer_support_router、document_summarizer、task_decomposer、long_form_writer、app_automation_workflow、dingtalk_checkin、dingtalk_checkout。

**置信度模型**: 种子 0.70，每次独立验证 +0.15/0.10/0.03，向 0.99 收敛。

**质量分**: `confidence × log₂(1 + usage_count) × (1 + rating/10)`

### 4.4 Builder Team

Builder 是多 Agent 系统（协调者 + 动态队友），通过逐个添加积木和连线搭建工作流。搭建前搜索模板，置信度 ≥0.7 优先展开。

### 4.5 元认知层

核心创新：从人-AI 协作中自动提取可复用工作流。

```
会话完成 → DecisionTracker → ExtractionGate（≥2决策？未被覆盖？有新分支？）
  → extract_workflow() → MergeEngine（相似度≥0.7？合并/新建）
```

提取门控三层过滤：最小决策点数、模板覆盖、新颖性。合并引擎通过 Jaccard（0.4）+ 深度（0.3）+ 边数（0.3）计算相似度。

## 5. 实验验证

### 5.1 测试覆盖（142 项 100%）

| 套件 | 项目数 | 通过率 |
|------|--------|--------|
| 单元测试 | 40 | 100% |
| 结构评估 | 49 | 100% |
| 专家测试 | 35 | 100% |
| 生产增强 | 18 | 100% |

### 5.2 多 Agent 代码生成

Coder + Tester → 536 行 + 76 测试。**76/76 全部通过。**

### 5.3 模板展开速度：比从零搭建快 **2,292 倍**。

### 5.4 非确定性隔离

3 次运行 LLM 输出长度 [175, 158, 150] 不等，结构断言全部通过。确定性工作流 5 次输出完全一致。

### 5.5 并发安全：5+10 并发，零污染，零失败。

### 5.6 模型升级验证

DeepSeek V4 Pro `xhigh` vs `high`：Builder -35%，Agent Prompt +73%，JSON 截断消除。**积木架构零变更。**

## 6. 讨论

范式适用范围、当前局限与未来工作详见 v2 姊妹篇。

## 7. 结论

Harness+LLM 复合体是所有 Agent 系统的不可再分结构不变量。Lilies 通过 41 积木、模板市场和元认知层证明了范式可实现。142 项测试 100% 通过证明了确定性 Harness 和非确定性 LLM 可以在单一可测试系统中共存。

**原语即耦合。其余皆为粒度的选择。**

---

## 参考文献

[1] Anthropic. Claude Code, 2025. [2] MCP, 2024. [3] Dify, 2024. [4-21] 完整引用见姊妹篇。

---

*此版本 (v1 中文) 为 Lilies 中心版本。范式中心版本 (v2 中文) 请见 THE_PRIMITIVE_IS_THE_PAIR_CN.md*
