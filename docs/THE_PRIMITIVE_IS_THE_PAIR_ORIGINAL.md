# Harness + LLM 复合体是 Lilies 的原子能力单元

> 关于积木优化为何必然陷入"先有鸡还是先有蛋"循环，以及这对后续设计的指导意义。

---

## 1. 核心洞察

### 1.1 任何积木优化最终都会变成工作流的副本

我们尝试了三个积木优化方向：

| 方向 | 方案 | 内部结构 |
|------|------|---------|
| Builder 配置增强 | 让 Builder 生成更精确的 JSON config | Builder (硬) + Prompt中的LLM (软) |
| Prompt 模式 | 在 IfElseConfig 中加 `route_prompt` | JSON Schema (硬) + LLM解析 (软) |
| SmartBlock | 新积木类型，硬控制流 + 软语义 | control字段 (硬) + instruction字段 (软) |

**三者的内部结构完全相同**：硬的执行框架 + 软的语义注入。

而这就是 Lilies 工作流本身的模式。一个工作流（如 `customer_support_router`）的 DAG：

```
[question_classifier] → [if_else] → [template_a / template_b]
```

也是硬的 Harness（积木拓扑 + JSON Schema） + 软的 LLM（classifier 内部的语义理解）。

**如果把 DAG 的智能藏进单个积木，你得到的是同一个东西——只是塞进了一个更小的壳里。**

### 1.2 鸡和蛋是同一种东西

```
鸡 (工作流):   已经能表达 Harness+LLM 复合体，在 DAG 层面
蛋 (积木优化): 试图在积木内部创造同样的复合体

蛋孵化出来的东西，是同一只鸡——只是藏在了一个更小的壳里。
```

## 2. Harness + LLM 复合体是原子单元

### 2.1 定义

Lilies 中任何有意义的能力都由两部分组成：

```
能力 = 执行框架 (确定、可测试、可组合) + 语义推理 (灵活、适应、理解)
     = Harness (硬)                            + LLM (软)
```

**这个复合体是不可再分的。** 你不能只靠 Harness（纯 JSON Schema 没有语义理解能力），也不能只靠 LLM（纯 LLM 输出没有确定性保证）。

### 2.2 在不同抽象层次的实例化

| 层次 | Harness 部分 | LLM 部分 |
|------|-------------|---------|
| 单个积木 | JSON Schema config | model_turn 的 system prompt |
| 积木组合 | DAG 拓扑结构 | 积木内的 LLM 调用 |
| 工作流模板 | 固定的积木链 | Builder 对需求的理解 |
| Agent Factory | AgentSpec 的 JSON Schema | DeepSeek 生成 AgentSpec |
| 元认知层 | DecisionTracker 的数据结构 | Review Agent 分析对话 |
| Platform Harness | JWT, Docker cgroup, 配额 | 无（纯硬约束） |

**每一层都是同一个复合体模式的不同实例。**

### 2.3 与 Dify 和 LangGraph 的对比

| 系统 | 复合体的放置位置 |
|------|----------------|
| Dify | 每个节点内部（节点 = 积木 + 内嵌 LLM 决策） |
| LangGraph | Python 代码中（State 对象 + 任意 LLM 调用） |
| Lilies | **DAG 的拓扑结构中**（积木是纯软的 LLM/纯硬的工具 + 连接关系） |

Lilies 的选择是：**不在积木内部放 LLM 决策，而是让 LLM 决策表现为积木之间的连接关系。**

## 3. 六个推论

### 推论 1: 积木永远不需要变聪明

积木的角色是提供确定的、可测试的、可组合的运行时机制。积木的"智能"来自它们在 DAG 中的位置和连接关系。

```
积木 = 乐高砖块（硬，形状固定）
积木组合 = 建筑（软，无限可能）

你不需要让砖块变聪明。
你需要的是更好的建筑图纸（模板）+ 更好的建筑师（Builder）。
```

### 推论 2: 所有优化应该投向组合层，而非积木层

| 错误方向 | 正确方向 |
|---------|---------|
| 让 if_else 能自己决定路由规则 | 让 Builder 更快找到 `classifier → if_else` 组合 |
| 给 context_assembler 加 LLM 拼接能力 | 让元认知层从成功搭建中提取 context 处理模式 |
| 创建能自动选择 strategy 的 SoftBlock | 让模板系统在下次相似需求时直接推荐匹配模板 |

### 推论 3: 积木数量的上限已经到达

25 + 16 = 41 个积木覆盖了 15 项 Harness 能力。

新增积木的**唯一合理来源**：
- Harness 层出现新能力（如 `hook_point`）
- 出现真正新的运行时机制——不是语义变体，而是新的执行原语

**不存在"更好的 if_else"——只存在"if_else 在更好的工作流中的更好的用法"。**

### 推论 4: 模板质量 > 积木质量

平台的长期竞争力来自三个飞轮的转速：

```
发现飞轮:  用户需求 → Builder 搜索 → 模板匹配 → 展开 → 定制
提取飞轮:  搭建成功 → auto-extract → Gate → Merge → 置信度递增
推荐飞轮:  高置信度模板 → 优先推荐 → 更多使用 → 置信度更高 → 推荐更准
```

这三个飞轮都不依赖积木本身的改进。

### 推论 5: 软硬分层是正确的架构选择

```
Layer 3 (软):  模板市场 + 元认知层
              ↑ 自然语言理解的领域
              
Layer 2 (半软): Builder Team + Agent Factory  
              ↑ LLM 推理的领域
              
Layer 1 (硬):  25 积木 + Workflow DAG + JSON Schema
              ↑ 确定性执行的领域
```

**每一层有明确的职责。不跨层优化。**

这就是为什么 `auto_strategy` 在 Layer 1 是错误的——它把 Layer 3 的软能力塞进了 Layer 1 的硬积木，制造了语义重复和架构矛盾。

### 推论 6: "更好的 LLM"是唯一能提升积木组合质量的外部因素

```
更强大的 LLM
  → Builder 生成的配置更精确
  → Agent Factory 生成的 Agent 更可靠
  → 元认知层提取的决策树更准确
  → 模板置信度增长更快
  → 所有飞轮加速

积木本身不需要变。
```

**这验证了 Lilies 的核心设计承诺：工作流是模型无关的资产。换一个更好的 LLM，整个平台的能力同时提升，而积木架构完全不变。**

## 4. 对架构决策的影响

### 4.1 验证已有 ADR

| ADR | 决定 | 本文推论 |
|-----|------|---------|
| ADR-001 | 积木粒度不合并 | 推论 1、3 |
| ADR-002 | 模板采用 Fork 模型 | 推论 4 |
| ADR-003 | Hook Point v1 非阻塞 | 推论 5 (Layer 1 不跨层) |
| ADR-008 | structural_only 模式 | 推论 5 (确定性隔离) |

### 4.2 否决的方向

| 方向 | 否决理由 |
|------|---------|
| 积木内嵌 LLM 决策 (auto_strategy) | 违反推论 1、5：把 Layer 3 能力塞进 Layer 1 |
| 创建"万能积木" (SoftBlock with runtime selection) | 违反推论 1、3：积木不需要变聪明 |
| 减少积木数量到 < 25 | 违反推论 3：25 个已是最小完备集 |

### 4.3 建议的方向

| 方向 | 优先级 | 依据 |
|------|--------|------|
| 模板置信度驱动的推荐飞轮 | P0 | 推论 4 |
| Builder → TemplateStore 全功能连接 | P0 | 推论 2 |
| auto-extract 元认知闭环 | P0 | 推论 2、4 |
| 模板使用统计 + 评分 | P1 | 推论 4 |
| 多 Provider (推论 6 的实证) | P1 | 推论 6 |

## 5. 设计检视清单

在评审任何新功能提案时，回答以下三个问题：

1. **这个改变是在哪一层？** (Layer 1 硬 / Layer 2 半软 / Layer 3 软)
2. **是否把软的智能藏进了硬的壳里？** (如果是 → 否决。用 DAG 表达，不用积木内部表达。)
3. **是否让飞轮转得更快？** (如果不是 → 低优先级。)

---

*本文档与 [DESIGN_RATIONALE.md](./DESIGN_RATIONALE.md) 互补。DESIGN_RATIONALE 记录"我们做了什么和为什么"；本文档记录"我们不做什么和为什么不"。*
