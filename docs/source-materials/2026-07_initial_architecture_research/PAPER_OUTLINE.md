# The Primitive Is The Pair: Harness+LLM as the Atomic Unit of Agent Architectures

> 论文大纲 · 目标期刊: ICSE/ASE 工具论文 或 OOPSLA/Onward! 范式论文

---

## 摘要 (Abstract)

当前 AI Agent 系统领域缺乏一个公认的内涵式定义。"Agent"在不同框架中指代完全不同的粒度——从单个 LLM 调用到完整的多轮自主系统。本文提出一个统一的范式：**任何 Agent 系统的原子能力单元都是 Harness+LLM 复合体**。Harness 提供确定性的执行框架（可测试、可组合、可验证），LLM 提供非确定性的语义推理（灵活、适应、理解）。这个复合体不是"Agent 的定义"，而是"所有被称作 Agent 的东西的共同结构"。它在不同粒度上反复出现——从单个积木到完整的工作流平台。我们以 Lilies 系统为验证载体，展示了该范式在 41 个积木、三层架构、元认知层和多 Agent 软件工程中的实例化效果，并通过 76/76 测试用例和端到端代码生成验证了其工程可行性。

## 1. Introduction (引言)

### 1.1 The Agent Definition Problem

- 当前缺乏公认的内涵式 Agent 定义
- 外延式定义的困境：AutoGPT、LangGraph、Dify、Claude Code 各自定义不同粒度的 Agent
- 争论不休的根因：用外延（列举属性）而非内涵（指出结构）来定义

### 1.2 本文贡献

1. **一个统一范式**：Harness+LLM 复合体是所有 Agent 系统的最小公分母
2. **三个推论**：关于积木不变、组合优化、模型无关性的工程指导
3. **一个验证系统**：Lilies — 实现了该范式从原子积木到元认知层的全谱系
4. **实验证据**：多 Agent 代码生成、模板提取、非确定性隔离的定量结果

## 2. The Harness+LLM Composite (范式描述)

### 2.1 Definition

```
能力 = 执行框架 (Harness) + 语义推理 (LLM)

Harness: 确定、可测试、可组合、可验证
LLM:     灵活、适应、理解、非确定
```

### 2.2 为什么是最小公分母

- 你可以缺 Harness（纯 LLM→不可测试）
- 你可以缺 LLM（纯代码→无语义理解）  
- 但同时拥有两者是所有"Agent"系统的共同特征
- 不是"Agent 的定义"，而是"Agent 的结构不变量"

### 2.3 粒度独立性

同一复合体在不同粒度上的实例化：

| 粒度 | Harness | LLM | 示例 |
|------|---------|-----|------|
| 积木级 | JSON Schema + 配置 | model_turn 的 system prompt | question_classifier |
| 组合级 | DAG 拓扑结构 | 积木内的 LLM 调用 | 14-block Agent Loop |
| 模板级 | 固定的积木链 | Builder 对需求的理解 | code_reviewer |
| 系统级 | 41 积木 + DAG 引擎 | Builder + AgentFactory | Lilies 本身 |
| 平台级 | JWT, Docker cgroup | (无 LLM — 纯硬约束) | Platform Harness |

### 2.4 与现有框架对比

| 框架 | 复合体的放置位置 | 粒度 |
|------|----------------|------|
| LangGraph | StateGraph 中 | 图 = Agent |
| Dify | 每个节点内部 | 节点 = Agent |
| AutoGPT | 主循环中 | 循环 = Agent |
| Lilies | DAG 拓扑的连接关系中 | 拓扑中的位置 = Agent |

## 3. Architectural Consequences (工程推论)

### 3.1 积木不需要变聪明

- 积木提供 Harness（确定），组合提供智能（LLM 在拓扑中）
- 不需要"更好的 if_else"——需要"if_else 在更好的工作流中"

### 3.2 组合层优化优先于积木层优化

- 正确的优化方向：模板质量、Builder Prompt、元认知提取
- 错误的优化方向：积木内嵌 LLM 决策（auto_strategy 反模式）

### 3.3 模型无关性

- 换一个更好的 LLM = 所有能力同步提升 = 积木架构不变
- 实验验证：DeepSeek V4 Pro xhigh vs high — Builder 速度 +35%，Agent Prompt +73%

## 4. The Lilies System (验证载体)

### 4.1 三层架构

```
Layer 3 (软):   模板市场 + 元认知层       ← 自然语言理解
Layer 2 (半软):  Builder + AgentFactory    ← LLM 推理
Layer 1 (硬):    41 积木 + DAG + Schema    ← 确定性执行
Platform:        JWT, Docker, Quota        ← 硬约束
```

### 4.2 41 积木系统

- 16 业务工作流积木 + 25 Agent 架构积木
- 每个对应 Agent 系统中一个可观察的运行时机制（全部为原创 Python 实现）
- 覆盖 15 项 Harness 能力

### 4.3 模板市场

- 9 个内置模板（代码审查、客服路由、数据分析...）
- Fork 模型（展开→修改→发布回市场）
- 置信度模型（种子 0.70 → 验证 +0.15 → 多次 0.98）

### 4.4 元认知层 (Meta-Cognition)

- **核心创新**：从人+AI 协作中自动提取工作流模板
- DecisionTracker → ExtractionGate → MergeEngine → TemplateStore
- 三个飞轮：发现（搜索→匹配→展开）/ 提取（auto-extract→Gate→Merge）/ 推荐（置信度↑→优先推荐）

## 5. Experimental Validation (实验验证)

### 5.1 测试覆盖

| 套件 | 项目 | 通过率 | 关键验证 |
|------|------|--------|---------|
| 单元测试 | 40 | 100% | Agent Runtime, Workflow DAG, 积木系统 |
| 结构评估 | 49 | 100% | 积木链、DAG、错误、并发、确定性 |
| 专家测试 | 35 | 100% | 嵌套、故障注入、权限矩阵、组合爆发 |
| 生产增强 | 18 | 100% | 非确定性隔离、并发、Checkpoint |
| 元认知 | 验证通过 | — | 决策树→工作流自动转换 |

### 5.2 多 Agent 代码生成

- 任务：构建 JSON Schema 验证器库（8 种 schema 关键词）
- Agent 链：Coder → Tester
- 产物：536 行生产代码 + 76 个 pytest 测试
- 结果：**76/76 全部通过**
- 冒烟测试：valid data, missing required, type mismatch — 全部正确

### 5.3 模板展开 vs Builder 搭建

- 同一需求：模板展开 <1s，Builder 搭建 ~125s
- 模板快 2292x，且结构保证正确

### 5.4 非确定性隔离

- structural_only 模式：3 次运行答案长度 [175, 158, 150] 各不相同
- 结构断言（exists+min_length+type）：3/3 全部通过
- 纯确定性流程：5 次运行输出完全一致

## 6. Related Work (相关工作)

### 6.1 Agent 框架

- LangGraph (LangChain): StateGraph + Node，复合体在图中
- Dify: 节点即为 Agent，复合体在节点内部
- AutoGPT / BabyAGI: 主循环模式，复合体在循环中
- Claude Code: Harness+Agent 分离，Lilies 的灵感来源

### 6.2 Workflow as Code / Low-Code

- Temporal, Airflow, Prefect: 确定性 Workflow，缺少 LLM 语义层
- n8n, Zapier: 可视化工作流，无 AI 搭建能力

### 6.3 Meta-Learning / AutoML

- AutoML 的搜索空间优化 vs Lilies 的工作流模板提取
- 区别：Lilies 提取的是流程结构，不是模型超参数

## 7. Discussion and Future Work (讨论与展望)

### 7.1 范式的边界

- Harness+LLM 复合体是否涵盖所有 AI 系统？（纯 ML 推理→无 LLM，不在范围内）
- 当 LLM 被替换为确定性推理（规则引擎）时，复合体退化为纯 Harness
- Platform Harness 层无 LLM — 它是"Agent 之上的硬约束"

### 7.2 推荐的未来方向

1. **多 Provider 验证推论 6**：换 LLM→同架构→不同质量
2. **模板飞轮闭环**：usage_count / rating / quality_score 全链路数据驱动
3. **跨框架模板互操作**：能否从 LangGraph workflow 提取 Harness+LLM 复合体？
4. **自动化架构决策**：让元认知层自己决定合适的 Harness+LLM 粒度

## 8. Conclusion (结论)

本文提出 Harness+LLM 复合体作为 Agent 系统的原子能力单元。该范式不是"Agent 的定义"，而是"所有被称作 Agent 的东西的共同结构不变量"。通过对粒度独立性、积木不可变性和组合层优化的系统分析，我们展示了该范式在工程上的指导力。Lilies 系统作为验证载体，证明了从原子积木到元认知层的全谱系实现是可行的，并通过 76/76 测试用例和端到端代码生成提供了定量的实验证据。

---

## 附录: 建议的提交策略

| 维度 | 建议 |
|------|------|
| **目标会议** | 一投: ICSE 2027 NIER (New Ideas) / 二投: OOPSLA Onward! 2027 |
| **论文类型** | 范式论文 (Vision/Position Paper) + 工具论文 (Tool Demo) |
| **核心卖点** | 不是 "我们做了另一个 Agent 框架"，而是 "Agent 框架的底层结构不变量" |
| **区别于 LangGraph/Dify 论文** | 我们不做 Agent，我们定义 Agent 的结构 |
| **实验部分** | 76/76 测试 + 模板展开 ×2292 = 可量化的工程成果 |
| **开源** | Lilies 完整代码 + 模板 + 测试套件可作为 artifact 提交 |
