# asset_cluster_pair_architecture

**分类**：架构模式

**来源阶段**：v0.4.x（集群协作需求驱动）

**关联资产**：[[asset_harness_llm_composite]] [[asset_platform_harness_task_monitor_boundary]] [[asset_theoretical_review]]

---

## 核心结论

大规模 Agent 集群协作不应通过新建一个"集群系统"来实现，而应通过**分形组合 The Pair** 来构建。每一层都是同一个模式——Harness（确定性保证）+ LLM（非确定性决策）——的不同实例化。

```
Agent Pair  →  Group Pair  →  Cluster Pair
(个体智能)     (组内协作)      (全局涌现)

每一层都是同一个模式，通过组合放大
```

## 三层结构

| 层级 | Harness 职责 | LLM 职责 | 规模 |
|------|-------------|---------|------|
| Agent Pair | Docker 沙箱、预算门、checkpoint、权限 | 个体推理、工具调用、决策 | 1 |
| Group Pair | 消息持久化、有序投递、冲突检测、成员发现 | 协作策略、任务分配、协商 | 10-100 |
| Cluster Pair | 跨组路由、故障恢复、分区容错 | 全局优化、自适应重组 | 100-10000 |

## 关键设计原则

### 1. 通信是 Harness，不是 LLM

Agent 之间的消息传递必须是确定性的——持久化、有序、去重、可审计。LLM 只决定"给谁发"、"发什么"和"收到后怎么做"，不参与消息传输本身。

**反模式**：让 Agent 通过 LLM 生成的自由文本来协商（导致幻觉级联、不可调试）。

### 2. 涌现来自约束 × 自由 × 规模

```
涌现 = 确定性边界 (Harness) × 非确定性决策 (LLM) × Agent 数量
```

- 边界太紧（纯规则系统）→ 无涌现
- 边界太松（纯 LLM 自由通信）→ 混乱
- **最佳点**：Harness 保证安全 + LLM 提供智能 + 足够多的 Agent

### 3. 分形组合优于层级继承

不是"集群系统继承消息系统继承 Agent 系统"。每个 Pair 是独立的、可替换的实例。Group Pair 的 Harness（消息总线）可以独立替换为 Redis/NATS，不影响 Agent Pair。

### 4. 确定性消息总线是最小可行 Harness

对于 100-1000 Agent 级别的集群，**SQLite WAL + 独立游标** 提供了足够的能力，不需要引入外部消息队列。这种极简设计验证了一个重要命题：**Harness 的复杂度应该与所需保证成正比，而非预先过度设计。**

## 可复用模式

### 模式 A：独立游标多播

```
Publisher → Topic (有序序列)
              ↓ cursor_A → Subscriber A  (独立进度)
              ↓ cursor_B → Subscriber B  (独立进度)
              ↓ cursor_C → Subscriber C  (独立进度)
```

比传统消息队列的 consumer group 更简单：每个订阅者完全独立，不需要 ack，不需要 rebalance。

### 模式 B：悲观冲突预防

```
Agent 操作共享资源前的强制检查点：
  1. acquire(resource, mode) → True/False
  2. 执行业务逻辑
  3. release(resource) 或 TTL 自动过期
```

比乐观锁（事后检测冲突）更适合 Agent 场景：Agent 之间的协作代价高，事前避免冲突的成本低于事后修复。

### 模式 C：能力注册发现

```
Agent 启动 → register(capabilities)
Agent 寻找协作者 → discover("image_analysis") → [agent_A, agent_B]
Agent 退出 → unregister (或心跳超时 → offline)
```

LLM 决策点：Agent 自主决定广告哪些能力、搜索哪些能力、发现后选择哪个协作者。

## 适用范围

### 适用场景
- 需要多个 Agent 异步协作的任务
- Agent 之间需要动态发现
- 共享资源需要冲突保护
- Map-Reduce、Scatter-Gather 等集群模式

### 不适用场景
- 确定性 DAG 工作流（用现有 DAG 编排更简单）
- 单 Agent 多步推理（用 AgentRuntime loop）
- 强实时要求（毫秒级延迟需要专用消息队列）
- 万级以上 Agent（需要分布式消息基础设施）

## 验证状态

- **设计级别**：H1（静态合约验证通过）
- **实现级别**：H2（组件沙箱验证通过：注册/发现/发布/订阅/冲突检测全部通过）
- **待验证**：H3（多 Agent 集成场景压力测试）

## 反模式警告

1. ❌ 不要用 cluster_* 积木替换简单的 DAG 编排
2. ❌ 不要让 Agent 之间通过 cluster_publish 发送未结构化的自由文本（应该发送结构化 payload）
3. ❌ 不要把 cluster_messaging 当作持久化存储（payload 应引用外部存储）
4. ❌ 不要绕过冲突检测直接操作共享资源
