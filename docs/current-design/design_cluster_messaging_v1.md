# design_cluster_messaging_v1

> ⚠️ **已被取代(2026-08)**:lean-core 已整体移除集群子系统。本文是集群时代的**历史设计参考**,不再作为工程依据;文中范畴论定理引用已撤回。保留以保存推导与历史。

## 1. 问题

Lilies 的多 Agent 能力局限于点对点的 DAG 编排（`subagent_spawn` + `task_dispatcher` + `mailbox`）。当面临"大规模智能体集群涌现协作"需求时，缺乏以下关键能力：

1. **多对多通信** — 现有 `mailbox` 纯点对点，无法广播或发布/订阅
2. **动态发现** — Agent 之间只能通过硬编码的节点引用协作，无法运行时发现
3. **冲突预防** — 多个 Agent 并发操作共享资源时无保护机制

核心设计问题：**如何在保持 The Pair（Harness + LLM）架构哲学的约束下，支持大规模 Agent 集群协作？**

## 2. 设计原则

来自 `asset_theoretical_review.md` 和 `asset_harness_llm_composite.md`：

> 每一层都是 Harness（确定性保证）+ LLM（非确定性决策）的复合体。智能从连接中涌现，而非从单个单元内部。

来自 `asset_platform_harness_task_monitor_boundary.md`：

> 所有消耗资源、可能长时间运行或调度执行的行为，都必须进入 task monitor boundary。

## 3. 架构设计

### 3.1 分形结构

```
层级           Harness (确定性)              LLM (非确定性)              规模
──────────────────────────────────────────────────────────────────────────────
Agent Pair     生命周期管理、沙箱隔离          决策、推理、工具调用           1
               预算门、权限门、checkpoint

Group Pair     消息路由、冲突检测、            组内协作策略、任务分配         10-100
               成员发现、负载均衡              优先级判断、资源协商

Cluster Pair   广播/多播、全局状态机、         涌现行为、全局优化、           100-10000
               故障恢复、分区容错              自适应重组、目标对齐
```

关键洞察：每一层都是同一个模式——Harness 提供确定性保证，LLM 在约束内做非确定性决策。层与层之间通过**组合**（不是继承、不是中心化调度）连接。

### 3.2 三层 Harness 实现

| 层级 | 组件 | 技术方案 |
|------|------|---------|
| Agent Pair | 现有 AgentRuntime + Docker 沙箱 | 已有 |
| Group Pair | ClusterMessageBus (SQLite pub/sub) | **新建** |
| Group Pair | ClusterRegistry (capability 注册中心) | **新建** |
| Group Pair | ConflictDetector (资源锁管理) | **新建** |
| Cluster Pair | 多个 Group 通过 topic 互联 | **组合** |

### 3.3 消息总线设计 (ClusterMessageBus)

```
┌──────────────────────────────────────────────────────┐
│  SQLite WAL 持久化存储                                │
│                                                      │
│  cluster_topics          cluster_messages             │
│  ┌──────────────┐       ┌──────────────────────┐    │
│  │ id: TEXT PK  │──1:N──│ id: TEXT PK           │    │
│  │ name: TEXT   │       │ topic_id: FK          │    │
│  └──────────────┘       │ publisher_id: TEXT    │    │
│                          │ payload: JSON TEXT    │    │
│  cluster_subscriptions   │ sequence: INT         │    │
│  ┌──────────────────┐   │ created_at: REAL      │    │
│  │ id: TEXT PK      │   └──────────────────────┘    │
│  │ topic_id: FK     │                               │
│  │ subscriber_id    │   每个订阅者维护独立游标         │
│  │ cursor_sequence  │   → 支持多消费者并行            │
│  └──────────────────┘                               │
└──────────────────────────────────────────────────────┘
```

关键设计决策：
- **SQLite 而非 Redis**：零外部依赖，Lilies 已使用 SQLite WAL
- **独立游标**：每个订阅者有自己的 cursor_sequence，互不干扰
- **幂等发布**：payload 中的 `_msg_id` 去重，相同 ID 返回原消息
- **有序保证**：每个 topic 内的 sequence 严格递增

### 3.4 冲突检测设计 (ConflictDetector)

```
规则表：
  ┌──────────┬───────────┬───────────┐
  │ 请求     │ 已有 Read │ 已有 Write │
  ├──────────┼───────────┼───────────┤
  │ Read     │ ✅ 允许   │ ❌ 冲突    │
  │ Write    │ ❌ 冲突   │ ❌ 冲突    │
  └──────────┴───────────┴───────────┘

TTL 机制：
  - 默认 300 秒 TTL
  - 过期自动清理 → 防止死 Agent 永久持锁
  - 支持 refresh 延长 → 长时间运行的任务
  - 只有 owner 可以 release
```

## 4. 积木设计

6 个新积木，全部归类为 `integration`：

| 积木 | 输入 | 输出 | 重试 | 说明 |
|------|------|------|------|------|
| `cluster_publish` | topic, publisher_id, payload | msg_id, sequence | ✅ | 发布消息 |
| `cluster_subscribe` | topic, subscriber_id, timeout | messages[] | ✅ | 订阅/等待 |
| `cluster_register` | agent_id, capabilities | status | — | 注册能力 |
| `cluster_discover` | capability | agents[] | — | 发现 Agent |
| `cluster_acquire` | resource_id, mode, ttl | acquired | ✅ | 获取锁 |
| `cluster_release` | resource_id | released | — | 释放锁 |

## 5. API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/v1/cluster/topics` | GET | 列出所有 topic 及消息计数 |
| `/api/v1/cluster/topics/{topic}/messages` | GET | 查看 topic 中的消息历史 |
| `/api/v1/cluster/registry` | GET | 按 capability 发现活跃 Agent |
| `/api/v1/cluster/locks` | GET | 查看所有活跃资源锁 |

## 6. 实现边界

- **目标**：提供 Group Pair 级别的基础设施（消息总线 + 注册中心 + 冲突检测），使 Builder 可以用积木组合出多 Agent 协作工作流
- **非目标**：不实现 Cluster Pair 级别的分布式调度；不引入外部消息队列；不替代 AgentRuntime
- **边界**：SQLite 的并发能力限制在单机 100-1000 Agent 级别。真正的分布式集群需要升级到 Redis/NATS。当前设计为 future-proof 保留了接口抽象

## 7. 与现有代码的关系

```
cluster_messaging.py  ──Harness 基础设施──▶  WorkflowRuntime._ensure_cluster()
cluster_blocks.py     ──Block 定义────────▶  blocks.py (build_block_registry)
workflow_runtime.py   ──运行时 handler────▶  6 个 isinstance 分发
api.py                ──监控 API──────────▶  app.state.workflow_runtime
```

零破坏性变更：所有新增代码通过新增文件 + 追加 handler 实现，不修改现有积木行为。
