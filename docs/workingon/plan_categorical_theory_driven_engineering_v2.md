# plan_categorical_theory_driven_engineering_v2

## 1. 状态：📋 阶段二规划 (2026-07-24)

本文档是 `plan_categorical_theory_driven_engineering_v1.md` 的延续与深化。在阶段一完成 3947 行代码、25 个集成测试、Task Market 多 Agent 集群原型验证的基础上，本文档以完整且严格的视角，对阶段一中暴露的 4 个设计权衡和 5 个场景盲区逐一给出范畴论分析，并推导出阶段二的工程路线图。

**前置阅读**：
- `plan_categorical_theory_driven_engineering_v1.md` — 阶段一交付与实证数据
- `docs/source-materials/2026-07_initial_architecture_research/the_pair_categorical.tex` — 20 个定理的权威形式化
- `docs/intellectual-assets/asset_cluster_minimality_proof.md` — L0/L1/L2 谱系的严格证明

**核心原则**（贯穿全文）：

> *The Pair is a monad. Workflows are its algebras. Clusters are its fixed point. Everything else is Yoneda.*

---

## 2. 理论基座：从定理到工程判据

本节建立 8 个核心定理与工程行动之间的精确定向映射。每个映射包含三个要素：定理陈述、工程判据、违反后果。

### 2.1 不动点定理（Thm 7.1）

> **定理**：$\pairMonad(\mathrm{level}_2) = \mathrm{level}_2$。Pair 函子应用两次迭代后到达不动点。不存在 level_3+。

**工程判据**：任何设计提案若声称需要"集群的集群"、"super-cluster"、"跨集群协调层"，除非能证明该提案不是新架构层级（而是 level_2 内部的水平分片），否则应被拒绝。

**违反后果**：引入 level_3 等价于在范畴中构造 $P^3 \not\cong P^2$，违反了公理 3。这会导致层级数无理论上界、调试跨度不可预测（参比：LangGraph Supervisor 嵌套的著名问题）。

**实现约束**：
```
✅ 多个独立 ClusterMessageBus 实例通过约定 topic 前缀互联（分片）
✅ 跨集群路由 = cluster_publish 到 __shard_X.<topic>
❌ 新建 super_cluster_* 积木系列
❌ 在 WorkflowSpec 中增加 cluster_of_clusters 层级
```

### 2.2 Det 闭包定理（Thm 8.3）

> **定理**：子范畴 $\catDet$ 在顺序复合 $\circ$、张量积 $\otimes$ 和 trace $\mathrm{Tr}$ 下闭合。

**工程判据**：若一个处理路径上的所有态射属于 $\catDet$（不含 LLM 调用），则该路径的任意交织执行顺序产生相同的最终状态。乐观并发控制对该路径是安全的。

**违反后果**：对含 LLM 调用的路径使用乐观锁等价于假设 LLM 输出是可交换的——这是错误的，因为相同输入可产生不同输出。

**实现约束**：
```
✅ 对 Det-only 子图使用乐观并发（后置冲突检测 + 重试）
✅ 对含 LLM 子图使用悲观并发（前置 acquire）
✅ 判定可静态完成：检查节点类型是否在 DET_WHITELIST 中
❌ 对 NonDet 路径使用乐观并发
```

**判定白名单**（来自 Thm 8.3 证明）：
```python
DET_WHITELIST = {
    "start", "end", "schedule_trigger",
    "if_else",
    "loop",                    # trace 算子本身是确定性的
    "template_transform",
    "variable_assigner",
    "variable_aggregator",
    "task_dispatcher",
}
```

### 2.3 L1 完全性定理（Thm 8.4）

> **定理**：$\{\mathrm{publish}, \mathrm{subscribe}, \mathrm{acquire}, \mathrm{release}\}$ 是实现并发安全的**完全集**。所有并发安全协议可归约到这 4 个积木。

**工程判据**：任何声称需要新并发原语的提案，必须首先证明该原语不能由 L1 的 4 个积木组合表达。如果可表达，应作为 Template 而非 block 实现。

**违反后果**：增加不必要的积木 → 积木数膨胀 → Builder 搜索空间碎片化 → 违反公理 2（不可约性）。

**已判定的可归约操作**：
```
broadcast    ≡ 多个 cluster_publish（Fan-Out）
rpc          ≡ cluster_publish + cluster_subscribe（await）+ timeout
shared_memory ≡ cluster_publish + cluster_subscribe 到同一 topic
negotiation  ≡ conditional_publish ∘ cluster_subscribe
barrier      ≡ cluster_publish + N × cluster_subscribe（poll_mode）
```

### 2.4 publish ⊣ subscribe 伴随定理（Thm 6.1）

> **定理**：$\mathrm{publish} \adjunction \mathrm{subscribe}$ 是跨工作流通通信的最小伴随对。伴随同构 $\homset{}{\mathrm{publish}(W_1)}{W_2} \cong \homset{}{W_1}{\mathrm{subscribe}(W_2)}$ 成立。

**工程判据**：Pull 和 Push 都是伴随对的合法实现。Pull 对应"subscribe 侧主动查询"，Push 对应"publish 侧触发通知"。两者可以共存。

**实现约束**：
```
✅ Pull 模式（当前实现）：subscribe → poll_messages / await_message
✅ Push 模式（可叠加）：publish → 唤醒阻塞的 await_message 订阅者（已实现 _wake_events）
✅ 混合模式：高优先级消息 push，低优先级消息 pull
❌ 放弃 pub/sub 伴随结构，改用紧耦合 RPC 作为唯一通信模式
```

### 2.5 模板 Monad 定理（Thm 5.2）

> **定理**：模板市场的所有合法操作恰好是 $\monadT$ 的 Kleisli 范畴 $\mathrm{Kl}(\monadT)$ 中的态射。

**工程判据**：Kleisli 范畴中自然存在的操作——`compose`、`specialize`、`diff`——应该在模板引擎中实现。当前仅实现了 `similarity`（diff 的雏形）。

**实现约束**：
```
✅ compose(T_A, T_B) → Template         — 两个模板的并行组合（张量积）
✅ specialize(T, context) → Template    — 基于运行时上下文的模板特化
✅ diff(T_A, T_B) → Patch               — 结构化差异，支持 apply_patch
```

### 2.6 自由构造定理（Thm 3.2）

> **定理**：由 CORE 生成的自由严格 monoidal category $F(\mathrm{CORE})$ 等价于纯计算工作流子范畴 $\catWFlow_{\mathrm{comp}}$。

**工程判据**：新积木增加的判定流程：在 $F(\mathrm{CORE})$ 生成范围内 → 便利积木（优先 Template 实现）；不在范围内 → 新生成元（需要引入新范畴结构）。

**违反后果**：便利积木被注册为 block → 积木数膨胀；新生成元被当作便利积木 → 缺失必要的 Harness 保证。

**判定流程**：
```
新积木提案
    │
    ├─ 可用 CORE + 已有积木的组合表达？
    │     YES → 便利积木
    │           优先：Template（复用 WorkflowSpec 组合）
    │           次选：block（仅当 Builder 搜索效率需要）
    │           注册时标注 derives_from: [源积木列表]
    │
    └─ NO  → 新生成元
             需要回答：引入了什么新的范畴结构？
               • 外部 I/O        → 需要（http_request, connector_action）
               • 跨工作流通信    → 需要（cluster_publish, cluster_subscribe）
               • 并发安全        → 需要（cluster_acquire, cluster_release）
               • 人机交互        → 需要（human_input）
             注册时标注 introduces: [新范畴结构]
```

### 2.7 Yoneda 嵌入定理（Thm 3.5）

> **定理**：一个 $ref 引用在语义上等价于 Yoneda 嵌入 $\Yoneda(n)$ 在指定路径上的求值。对象由其 presheaf 完全确定。

**工程判据**：架构的外部行为 = 它的 presheaf $\Yoneda(\mathrm{Arch}): \cat{Env}^{\mathrm{op}} \to \catSet$ — 即所有可能环境输入到所有可能响应的映射。不存在"隐藏的架构本质"——架构就是它的交互界面。

**实现约束**：
```
✅ 每个外部集成点（connector, HTTP endpoint, human_input）必须精确定义
   其 Hom 集合（输入空间 + 输出空间 + 合法性判据）
✅ 测试完备性 = 在 presheaf 空间中的充分采样
✅ 功能等价判据：两个版本的部署行为等价 ⇔ 它们的 presheaf 自然同构
```

### 2.8 公理 3 独立性定理（Thm 8.5）

> **定理**：存在满足公理 1-2 但不满足公理 3（Pair 幂等）的范畴。公理 3 是独立选择，不能从公理 1-2 导出。

**工程判据**：Lilies 选择了公理 3，因此**显式拒绝**递归 Agent 嵌套模式。这不是能力不足——这是经过范畴论分析的架构决策。

**拒绝列表**：
```
提案                                 违反定理    替代方案
──────────────────────────────────────────────────────────────
"让 Agent 的输出本身是一个 Agent"     Thm 8.5    subagent_spawn（有边界、非递归）
"任意深度的 Supervisor 嵌套"         Thm 8.5    flat cluster + topic-based routing
"Agent 的 Agent 的 Agent"             Thm 7.1    不动点定理禁止 level_3+
"动态创建新 Agent 类型"               Thm 8.5    cluster_register（静态 capability）
```

---

## 3. 阶段一完成状态

### 3.1 代码交付

| 文件 | 行数 | 模块职责 |
|------|------|---------|
| `cluster_messaging.py` | 783 | L1 完备性原语：conditional_publish, heartbeat, expire_inactive_agents, lock_upgrade, lock_holders, peek_messages。MessageBus, Registry, ConflictDetector |
| `cluster_blocks.py` | 245 | 6 个集群积木的 Pydantic config + Editor fields + Builder manuals |
| `cluster_telemetry.py` | 377 | Lamport 时钟事件日志，消息边/锁竞争/交互摘要提取 |
| `cluster_runner.py` | 576 | 确定性多 Agent 场景运行器，6 种内置决策函数工厂 |
| `cluster_analysis.py` | 485 | 4 消息流模式 + 4 锁竞争模式 + 4 涌现信号 + 5 项定理自动验证 |
| `examples/cluster_task_market.py` | 663 | Task Market 可运行实例 |
| `tests/test_cluster_l1.py` | 818 | 25 个集成测试，6 个测试类 |
| **总计** | **3947** | |

### 3.2 测试覆盖

25 个测试全部通过。分层覆盖如下：

| 层 | 测试数 | 关键验证项 |
|----|--------|----------|
| L1 原语 | 6 | conditional_publish (exclusive_window, no_recent_from), heartbeat+expiry, lock_upgrade, lock_holders |
| 基本场景 | 6 | pubsub 两 Agent, fan-out 一对多, 资源竞争, 协商, heartbeat 保活, 确定性可复现 |
| 模式检测 | 3 | fan-out 检测, hot resource 检测, 文本报告生成 |
| 定理验证 | 4 | L1 完备性, 无死锁, Det 闭包, 遥测完整性（Lamport 时钟序） |
| 并发压力 | 2 | 10 Agent 并发 pub/sub (零消息丢失), 5 Agent 并发锁获取/释放 |
| 边界情况 | 4 | 空场景, 单 Agent, topic 历史, 幂等发布 |

### 3.3 Task Market 实证数据

| 指标 | 数值 | 范畴解释 |
|------|------|---------|
| 任务发布 | 287 | Producer 持续注入环境输入（presheaf 查询） |
| 任务认领 | 63 | Worker 在消息空间中的自适应选择 |
| 锁冲突率 | 92.8% | 单写锁的必然代价（L1 完全性定理的工程表现） |
| Worker 负载分布 | 7-8/worker | 涌现均衡（fan-out 伴随结构的自然结果） |
| 检测到的模式 | 37 | 1 hot_resource + 8 starvation + 28 ping_pong |
| 定理验证 | 5/5 | L1 完备、Det 闭包、不动点、伴随、无死锁 |

---

## 4. 设计权衡：范畴论分析与解决方案

阶段一中有 4 个已知设计权衡。本节为每个权衡提供范畴论分析、工程方案和跨阶段不变量。

### 4.1 悲观锁 vs 乐观锁

**现状**：所有 Worker 使用悲观锁（前置 `acquire`），导致 92.8% 冲突率。

**范畴分析**：

Det 闭包定理（§2.2）提供了混合策略的严格判据——确定性态射在复合下保持确定性。这意味着：

- **Det-only 路径**：态射组合 $f_n \circ f_{n-1} \circ \cdots \circ f_1$ 中的每个 $f_i \in \catDet$。由 Thm 8.3，复合结果仍在 $\catDet$ 中。无论多少 Worker 以何种顺序交织执行，最终状态唯一。乐观并发控制在此路径上是安全的——冲突后重试的代价仅为计算资源，不影响正确性。

- **NonDet 路径**：至少存在一个 $f_i$ 包含 LLM 调用（$\notin \catDet$）。相同输入可产生不同输出，不同交织顺序可导致语义上不等价的结果。悲观并发控制是必要的。

**工程方案**：

```python
class LockStrategy:
    """Determined by static determinism analysis of the critical section."""

    async def protect(
        self,
        detector: ConflictDetector,
        resource_id: str,
        agent_id: str,
        critical_section: Callable[[], Any],
        determinism_map: dict[str, bool],  # from P0 determinism pass
    ) -> Any:
        if all(determinism_map.get(node_id, False)
               for node_id in self._nodes_in_critical_section):
            # Optimistic: Det-only path (Thm 8.3 guarantee)
            return await self._optimistic_execute(
                detector, resource_id, agent_id, critical_section
            )
        else:
            # Pessimistic: NonDet path
            return await self._pessimistic_execute(
                detector, resource_id, agent_id, critical_section
            )

    async def _optimistic_execute(self, detector, resource_id, agent_id, fn):
        """Execute first, detect conflict on commit."""
        result = fn()
        # Version-vector based conflict detection
        if not await detector.check_version(resource_id, self._read_version):
            return await self._optimistic_execute(...)  # retry
        return result

    async def _pessimistic_execute(self, detector, resource_id, agent_id, fn):
        """Acquire lock first, then execute."""
        acquired = await detector.acquire(resource_id, agent_id, "write")
        if not acquired:
            await asyncio.sleep(backoff)
            return await self._pessimistic_execute(...)  # retry
        try:
            return fn()
        finally:
            await detector.release(resource_id, agent_id)
```

**预期效果**：若 $D$ 个 Worker 中 $k$ 个处理 Det-only 路径，冲突率从 $\frac{D-1}{D}$ 降至 $\frac{D-k-1}{D-k}$。

**跨阶段不变量**：不论乐观/悲观策略如何混合，L1 完全性定理保证：所有并发安全协议仍可用 {P,S,A,R} 4 积木表达。

### 4.2 Pull vs Push

**现状**：Worker 通过 poll 模式获取消息，延迟 ≥ 1 轮。

**范畴分析**：

publish ⊣ subscribe 伴随关系（§2.4）不强制 Pull 或 Push。伴随同构的两种实现方式——Pull（从 subscribe 侧查询）和 Push（从 publish 侧通知）——在范畴上是等价的。当前代码中 `_wake_events` 已经实现了 Push 的基础设施：`await_message` 被 asyncio.Event 唤醒，延迟为 0。只是 Task Market 场景中的 Worker 使用了 `poll_messages` 模式，没有利用唤醒机制。

**工程方案**：Pull 和 Push 是互补的，不是互斥的：

```python
class HybridDeliveryMode:
    """
    Pull: subscriber controls consumption rate. Good for batch processing.
    Push: publisher triggers immediate delivery. Good for low-latency paths.
    """

    async def subscribe_with_mode(
        self, topic: str, subscriber_id: str,
        mode: Literal["pull", "push", "hybrid"],
        priority_threshold: str = "high",
    ) -> None:
        if mode == "push":
            await self._subscribe_push(topic, subscriber_id)
        elif mode == "pull":
            await self.bus.subscribe(topic, subscriber_id)  # current behavior
        else:  # hybrid
            await self.bus.subscribe(topic, subscriber_id)
            # High-priority messages: push (wake blocking subscribers)
            # Low-priority messages: pull (poll when ready)
            self._priority_wake_threshold[topic] = priority_threshold
```

**跨阶段不变量**：不论 Pull/Push 实现如何混合，publish ⊣ subscribe 伴随同构保持成立。

### 4.3 SQLite vs Redis/Kafka

**现状**：单 SQLite 文件，无分布式能力。

**范畴分析**：

这是最直接的函子抽象。L1 的 4 个原语定义了态射签名（接口），SQLite 是这些态射在 $\cat{SQLite}$ 范畴中的一个具体实现（一个具体函子 $F: \cat{L1} \to \cat{SQLite}$）。Redis 可以是另一个具体实现（$G: \cat{L1} \to \cat{Redis}$）。两者之间存在一个自然变换 $\eta: F \Rightarrow G$，保证接口语义不变，仅实现方式不同。

**工程方案**：定义传输层协议，L1 原语接口保持稳定：

```python
from typing import Protocol

class MessageTransport(Protocol):
    """L1 morphisms as a transport-agnostic protocol.

    This protocol IS the categorical interface. Each concrete implementation
    (SQLite, Redis, Kafka) is a functor from L1 to that transport category.
    """

    async def publish(self, topic: str, publisher_id: str,
                      payload: dict[str, Any]) -> ClusterMessage: ...

    async def subscribe(self, topic: str,
                        subscriber_id: str) -> str: ...

    async def await_message(self, topic: str, subscriber_id: str,
                            timeout: float) -> ClusterMessage | None: ...

    async def poll_messages(self, topic: str,
                            subscriber_id: str) -> list[ClusterMessage]: ...

    async def peek_messages(self, topic: str, subscriber_id: str,
                            limit: int = 100) -> list[ClusterMessage]: ...

    async def conditional_publish(self, topic: str, publisher_id: str,
                                  payload: dict[str, Any],
                                  condition: dict[str, Any]
                                  ) -> tuple[bool, ClusterMessage | None]: ...


class SQLiteMessageBus(MessageTransport):
    """Current implementation. Functor F: L1 → SQLite."""
    ...

class RedisMessageBus(MessageTransport):
    """Distributed implementation. Functor G: L1 → Redis."""
    ...

class KafkaMessageBus(MessageTransport):
    """High-throughput implementation. Functor H: L1 → Kafka."""
    ...
```

**L1 原语到 Redis 的直接映射**：

| L1 原语 | SQLite 实现 | Redis 实现 | 语义等价性 |
|---------|-----------|-----------|----------|
| `publish` | INSERT INTO cluster_messages | XADD topic * payload | 单调递增序列 |
| `poll_messages` | SELECT WHERE sequence > cursor | XREAD COUNT N STREAMS topic cursor | 独立游标 |
| `peek_messages` | SELECT WHERE sequence > cursor（不更新光标） | XREVRANGE topic + - COUNT N | 非消费读取 |
| `conditional_publish` | INSERT + SELECT in transaction | WATCH + MULTI/EXEC | 原子检查-写入 |
| `acquire` | INSERT OR REPLACE with conflict check | SETNX resource_id owner_id EX ttl | 排他性创建 |
| `subscribe` | INSERT OR IGNORE INTO cluster_subscriptions | XGROUP CREATE | 消费组注册 |

**跨阶段不变量**：不论传输层如何替换，L1 的 4 积木 + 2 组件的接口语义不变。Det 闭包定理和 L1 完全性定理在每个传输层实现中独立成立。

### 4.4 确定性调度 vs 真正并发

**现状**：固定轮次 + 固定 Agent 序，可复现但非真实并发语义。

**范畴分析**：

Det 闭包定理（§2.2）提供了关键保证：对于 Det-only 操作序列，任何交织执行顺序产生相同的最终状态。因此：

- 确定性调度记录的 Lamport 时钟序，与真正并发中 Det-only 部分产生的任意合法交织序，在 Det-only 子图上**等价**。
- 差异仅存在于 NonDet 操作（LLM 调用）：不同的交织序可能导致不同的 LLM 输出。这不是需要消除的错误——这是涌现的**真正非确定性**。

**工程方案**：确定性模式和并发模式共享相同的 Lamport 时钟序空间：

```python
class RunnerMode(Enum):
    DETERMINISTIC = "deterministic"  # round-based, fixed agent order
    CONCURRENT = "concurrent"        # asyncio tasks, natural scheduling


class ClusterScenarioRunner:
    def __init__(self, data_dir: Path, mode: RunnerMode = RunnerMode.DETERMINISTIC):
        self._mode = mode

    async def run(self, config, decisions):
        if self._mode == RunnerMode.DETERMINISTIC:
            return await self._run_deterministic(config, decisions)
        else:
            return await self._run_concurrent(config, decisions)

    async def _run_concurrent(self, config, decisions):
        """Each agent runs as an independent asyncio task.

        No round boundaries — agents act whenever they're ready.
        Lamport clock still produces causal ordering.
        Det closure theorem guarantees equivalence on Det-only subgraphs.
        """
        tasks = []
        for agent in config.agents:
            task = asyncio.create_task(
                self._agent_loop_concurrent(agent, config, decisions)
            )
            tasks.append(task)
        await asyncio.gather(*tasks)
```

**跨阶段不变量**：两种模式产生相同的 Lamport 时钟因果序。对于 Det-only 子图，两种模式的最终状态等价。

---

## 5. 场景盲区：结构性分析

阶段一中识别了 5 个场景覆盖盲区。本节为每个盲区提供范畴分析、可行方案和边界标注。

### 5.1 100+ Agent 大规模集群

**范畴分析**：

涌现可能性的组合空间论证给出：全局状态空间 $|S_{\mathrm{global}}| \geq O(2^{K \cdot T} \cdot N! \cdot 2^N)$。当 $N=100$ 时，穷举所有交互模式在计算上不可行。

但不动点定理（§2.1）提供了横向扩展的严格判据：**规模化必须是 level_2 内部的水平分片，不能是 level_3 的纵向升级。**

**工程方案 — ShardedMessageBus**：

```python
class ShardedMessageBus:
    """N 个独立 SQLite 实例，按一致性哈希路由。

    Each shard is a complete L1 instance. L1 theorems hold independently
    within each shard. Cross-shard communication: topic prefix convention.
    """

    def __init__(self, data_dir: Path, shard_count: int = 16):
        self._shards = [
            ClusterMessageBus(data_dir / f"cluster_shard_{i}")
            for i in range(shard_count)
        ]
        self._shard_count = shard_count

    def _route(self, topic: str) -> ClusterMessageBus:
        return self._shards[hash(topic) % self._shard_count]

    async def publish(self, topic: str, publisher_id: str,
                      payload: dict[str, Any]) -> ClusterMessage:
        return await self._route(topic).publish(topic, publisher_id, payload)

    # All other methods delegate similarly

    async def cross_shard_publish(self, from_shard: int, topic: str, ...):
        """Explicit cross-shard: topic naming convention.

        __shard_{from_shard}.{topic} → routed to target shard.
        """
        ...
```

**预期效果**：16 个分片，每个分片承载 ~6 Agent → 峰值冲突率从 92.8% 降至 ~83%（每个分片内部冲突率保持，但冲突被分片隔离）。

**边界标注**：跨分片一致性需要额外的协调协议。Lamport 时钟在跨分片时需要合并。分片间的 publish/subscribe 需要约定 topic 命名空间。

### 5.2 真实 LLM 驱动的自适应策略

**范畴分析**：

这是 Pair 模式最自然的下一步应用。当前 Worker 的决策函数签名是确定性的（硬编码评分规则）。替换为 LLM 驱动时，决策函数变为：

$$f_{\mathrm{decide}}: (\mathrm{agent\_id}, \mathrm{round}, \mathrm{observation}) \to \mathrm{AgentActionSpec}$$

这正是 Pair 结构：

$$f_{\mathrm{decide}} = H_{\mathrm{parse}} \circ L_{\mathrm{LLM}} \circ H_{\mathrm{prompt}}$$

其中 $H_{\mathrm{prompt}}$ 构建上下文，$L_{\mathrm{LLM}}$ 做语义决策，$H_{\mathrm{parse}}$ 将 LLM 输出解析为 AgentActionSpec。

**工程方案**：不需要改变任何基础设施：

```python
def make_llm_worker_decision(
    agent_id: str,
    capabilities: list[str],
    llm_provider,  # ModelProvider instance
) -> DecisionFn:
    """LLM-driven worker: observation → LLM → ActionSpec.

    Harness (deterministic): prompt construction, action parsing, type validation.
    LLM (non-deterministic): semantic decision of which action to take.
    """

    SYSTEM_PROMPT = """You are Worker {agent_id} in a multi-agent task market.

Your capabilities: {capabilities}

Available actions:
- SUBSCRIBE_POLL <topic>  — check for new tasks
- PUBLISH <topic> <payload> — claim a task or publish results
- ACQUIRE <resource> <mode> — get a lock before writing
- RELEASE <resource>        — release a lock
- IDLE                       — wait

Choose ONE action. Return JSON: {{"action": "...", "topic": "...", ...}}"""

    async def decide(agent_id_inner: str, round_num: int,
                     obs: AgentObservation) -> AgentActionSpec:
        # Harness: construct prompt (deterministic)
        prompt = SYSTEM_PROMPT.format(
            agent_id=agent_id_inner,
            capabilities=capabilities,
        )
        user_msg = json.dumps({
            "round": round_num,
            "pending_tasks": obs.pending_messages,
            "held_locks": obs.held_locks,
            "known_agents": obs.known_agents,
        })

        # LLM: semantic decision (non-deterministic)
        response = await llm_provider.chat(
            system=prompt,
            messages=[{"role": "user", "content": user_msg}],
            temperature=0.3,  # low but non-zero → adaptive but not chaotic
        )

        # Harness: parse and validate (deterministic)
        action_spec = _parse_action_spec(response.content)
        _validate_action_spec(action_spec, capabilities)
        return action_spec

    return decide
```

**预测**（范畴保证但需实证验证）：
1. LLM Worker 会产生 mock Worker 不会产生的差异化策略（自适应优先级判断）
2. 这些策略仍然全部在 L1 的 4 积木范围内（L1 完全性定理保证）
3. 涌现的**内容**会不同（更丰富的策略），但涌现的**结构**不会超越 level_2（不动点定理保证）

**边界标注**：LLM 调用的代价（延迟 + token 成本）是 mock 决策的 100-1000 倍。需要 budget_gate 防止成本失控。

### 5.3 跨机器分布式通信

**范畴分析**：

与 §4.3 相同。传输层替换是函子级别的操作——L1 接口保持稳定，实现从 `SQLiteMessageBus` 替换为 `RedisMessageBus`。两者的差异被自然变换 $\eta: F \Rightarrow G$ 捕获。

**工程方案**：

实现 `RedisMessageBus`（完整实现，不是概念代码）：

```python
class RedisMessageBus(MessageTransport):
    """L1 transport over Redis Streams.

    Mapping:
      publish   → XADD (append to stream)
      subscribe → XGROUP CREATE (consumer group)
      poll      → XREADGROUP (consumer group read)
      peek      → XRANGE/XREVRANGE (non-consuming read)
      conditional_publish → WATCH key + XADD in MULTI/EXEC
    """

    def __init__(self, redis_url: str, prefix: str = "lilies"):
        self._redis = aioredis.from_url(redis_url)
        self._prefix = prefix

    async def publish(self, topic, publisher_id, payload):
        key = f"{self._prefix}:topic:{topic}"
        msg_id = await self._redis.xadd(key, {
            "publisher_id": publisher_id,
            "payload": json.dumps(payload),
        }, id="*")
        return ClusterMessage(
            id=msg_id, topic=topic, publisher_id=publisher_id,
            payload=payload, sequence=int(msg_id.split("-")[0]),
            created_at=time.time(),
        )

    async def conditional_publish(self, topic, publisher_id, payload, condition):
        key = f"{self._prefix}:topic:{topic}"
        cond_key = f"{self._prefix}:cond:{topic}"

        async with self._redis.pipeline() as pipe:
            # WATCH the condition key
            await pipe.watch(cond_key)

            # Check condition (similar logic to SQLite version)
            if not await self._check_condition(pipe, topic, condition):
                await pipe.unwatch()
                return False, None

            # MULTI/EXEC — atomic from here
            pipe.multi()
            msg_id = pipe.xadd(key, {
                "publisher_id": publisher_id,
                "payload": json.dumps(payload),
            }, id="*")
            result = await pipe.execute()

            if result:
                msg_id_val = result[0]
                return True, ClusterMessage(
                    id=msg_id_val, topic=topic, publisher_id=publisher_id,
                    payload=payload, sequence=0, created_at=time.time(),
                )
            return False, None
```

**边界标注**：Redis 部署和运维复杂度显著高于 SQLite（零外部依赖 → 需要 Redis 集群）。建议在需要真正分布式能力（>10 Agent 跨机器）时才启用。

### 5.4 网络分区下的 CAP 行为

**范畴分析**：

当前框架隐含 CP（一致性 + 分区容错）假设——单个 SQLite 文件是唯一真相源。分布式场景下需要在 AP 和 CP 之间选择。publish ⊣ subscribe 伴随关系在分区下的行为：

```
无分区时：
  Hom(publish(W₁), W₂) ≅ Hom(W₁, subscribe(W₂))
  伴随同构完整成立

分区时：
  Hom(publish(W₁), W₂) → Hom(W₁, subscribe(W₂))
  单向蕴含——已发布的消息不一定能被分区另一侧的订阅者看到

这不对范畴框架构成反驳——它是对 CAP 定理在范畴语言中的重新表述。
```

**工程方案**：

Level_2 内部，选择 AP（可用性 + 分区容错）作为默认策略：

```python
class CAPStrategy:
    """Lilies cluster: AP by default within level_2.

    Rationale from categorical analysis:
    - publish ⊣ subscribe weakens to one-way implication under partition
    - Lamport clock still gives causal ordering
    - Application-level conflict resolution can repair inconsistencies
    """

    async def handle_partition(self, topic: str, local_events: list,
                               remote_events: list) -> None:
        """Merge events from both sides of a healed partition."""
        # Sort by Lamport clock (causal order preserved)
        all_events = sorted(
            local_events + remote_events,
            key=lambda e: e.lamport_clock,
        )

        # For lock conflicts across partitions:
        #   - Last-writer-wins by Lamport clock (deterministic)
        #   - Application-level callback for semantic resolution
        for conflict in self._find_lock_conflicts(all_events):
            resolved = self._resolve_by_clock(conflict)
            await self._notify_application(conflict, resolved)
```

**边界标注**：对于需要强一致性（如金融交易）的场景，应该使用 CP 传输层（如 etcd 或 ZooKeeper），而不是通用的 pub/sub 基础设施。这是架构边界的正确位置——不在 L1 层内混合 AP 和 CP。

### 5.5 Agent 异构计算资源调度

**范畴分析**：

当前能力模型使用扁平的 capability 标签。异质资源调度需要在能力范畴中引入 **enriched category** 结构——态射不仅存在或不存在，还携带代价、延迟、资源需求等度量。

这超出了当前范畴框架的范围（框架只关心**结构**，不关心**度量**），但框架提供了正确的扩展方向——在 `ClusterRegistry` metadata 中嵌入资源信息：

```python
await registry.register("worker_gpu_1", ["nlp", "generation"], metadata={
    "resources": {
        "gpu": "A100",
        "vram_gb": 80,
        "cost_per_1k_tokens": 0.02,
    },
    "performance": {
        "nlp": {"latency_ms": 500, "throughput_per_sec": 10},
        "generation": {"latency_ms": 2000, "throughput_per_sec": 3},
    },
    "availability": {
        "max_concurrent_tasks": 4,
        "current_load": 0.6,
    },
})
```

然后 `ClusterAnalyzer` 可以从遥测数据中提取资源利用模式，供调度决策使用。

**边界标注**：完整的资源调度算法（如 Kubernetes scheduler、Dominant Resource Fairness）超出了范畴框架的建模能力。当前的 enriched category 方向仅提供**数据模型指导**，不是完整的调度算法。

---

## 6. 阶段二工程路线图

基于以上分析，阶段二按优先级分为三个子阶段。

### 6.1 P0 — 传输层协议化 + Determinism Pass（1-2 周）

**范畴依据**：§4.3（SQLite → 传输层抽象）、§4.1（Det 闭包 → 混合锁策略）

**交付**：

| 任务 | 文件 | 说明 |
|------|------|------|
| MessageTransport 协议定义 | `cluster_messaging.py` | 将现有 7 个方法提取为 Protocol |
| SQLiteMessageBus 重构 | `cluster_messaging.py` | 实现 MessageTransport protocol |
| Determinism analysis pass | `blocks.py` | `BlockRegistry.is_deterministic(block_type) → bool`，基于 DET_WHITELIST |
| 确定性判定集成进 validate_workflow | `blocks.py` | `_analyze_determinism(workflow) → dict[str, bool]`，O(\|N\| + \|E\|) |
| 混合锁策略 | `workflow_runtime.py` | `LockStrategy.protect()` — Det-only → 乐观, NonDet → 悲观 |
| 测试：混合锁策略 | `tests/test_cluster_l1.py` | 验证 Det-only 路径零冲突，NonDet 路径保持悲观锁 |

**验收**：
- [ ] `MessageTransport` 协议定义完整，覆盖全部 7 个原语
- [ ] SQLiteMessageBus 通过协议检查（structural subtyping）
- [ ] `is_deterministic("llm") == False`
- [ ] `is_deterministic("template_transform") == True`
- [ ] Det-only Task Market 变体的锁冲突率为 0
- [ ] 已有 25 个测试无回归

### 6.2 P1 — LLM Worker + Redis Transport + Kleisli 操作（3-4 周）

**范畴依据**：§5.2（LLM 决策函数 = Pair 实例化）、§4.3（Redis 传输层）、§2.5（Kleisli 操作）

**交付**：

| 任务 | 文件 | 说明 |
|------|------|------|
| LLM Worker 决策函数 | `examples/cluster_task_market.py` | `make_llm_worker_decision()` — Pair 结构：Harness prompt + LLM 推理 + Harness parse |
| RedisMessageBus | `cluster_messaging.py` | 完整实现 MessageTransport protocol over Redis Streams |
| compose/specialize/diff | `merge_engine.py` | Kleisli 范畴的 3 个缺失操作 |
| LLM Task Market 场景运行 | 运行 + 遥测分析 | 对比 mock vs LLM Worker 的涌现差异 |
| Redis 集成测试 | `tests/test_cluster_l1.py` | 验证 Redis transport 与 SQLite transport 行为等价 |

**验收**：
- [ ] LLM Worker 可以自主完成 claim→acquire→publish→release 周期
- [ ] RedisMessageBus 通过与 SQLiteMessageBus 相同的 25 个测试（替换 transport fixture）
- [ ] `compose(T_A, T_B)` 正确合并节点集和边集
- [ ] `diff(T_A, T_B)` 产生的 Patch 可以 `apply_patch` 回 $T_A$ 得到语义等价的 $T_B$
- [ ] LLM Task Market 产生的涌现信号数量 ≥ mock 版本的 1.5 倍
- [ ] LLM Worker 的 token 成本在 budget_gate 限制内

### 6.3 P2 — Sharded Bus + ADR 形式化 + 压力测试（2-3 周）

**范畴依据**：§5.1（横向分片策略）、§2.8（拒绝列表 ADR）、涌现可能性证明（组合空间论证）

**交付**：

| 任务 | 文件 | 说明 |
|------|------|------|
| ShardedMessageBus | `cluster_messaging.py` | N 分片一致性哈希路由 |
| 架构决策记录（ADR） | `docs/current-design/adr_categorical_constraints.md` | 4 条拒绝列表的形式化记录 |
| 100 Agent 压力测试 | `tests/test_cluster_l1.py` | 16 分片，每个分片 6-7 Agent |
| 跨分片遥测分析 | 运行 + `cluster_analysis.py` | 验证分片间和分片内的涌现差异 |
| Redis transport 压力测试 | `tests/test_cluster_l1.py` | RedisMessageBus 在 100 Agent 下的吞吐和延迟 |

**验收**：
- [ ] 100 Agent 场景在 ShardedMessageBus 上无死锁
- [ ] 分片内冲突率与单实例相当；分片间无锁竞争（隔离保证）
- [ ] ADR 文档完整：4 条拒绝项，每条有定理引用 + 替代方案
- [ ] Redis transport 在 100 Agent 下吞吐 ≥ 1000 msg/s
- [ ] 跨分片 Lamport 时钟合并正确

---

## 7. 架构决策记录（ADR）：范畴约束的拒绝列表

以下提案被范畴论定理证明为冗余或有害，列入**永久拒绝列表**。每一项包含违反的定理、形式理由和替代方案。提案人如需推翻，必须首先在其提案中证明对应定理不适用于该提案。

### ADR-001：拒绝 "集群的集群"（level_3+）

- **违反定理**：Thm 7.1（不动点定理）
- **形式理由**：$\pairMonad(\mathrm{level}_2) = \mathrm{level}_2$。层级谱系在 level_2 饱和。任何声称需要 level_3 的提案等价于声称 $\pairMonad^3 \not\cong \pairMonad^2$，违反了公理 3。
- **替代方案**：多个独立 `ClusterMessageBus` 实例通过约定 topic 前缀（`__shard_N.topic`）互联，构成 level_2 内部的水平分片，而非 level_3 的纵向升级。

### ADR-002：拒绝 "递归 Agent 嵌套"

- **违反定理**：Thm 8.5（公理 3 独立性）
- **形式理由**：满足公理 1-2 但不满足公理 3 的反模型 $\catCap_{\mathsf{rec}}$（其中 $\pairMonad_{\mathsf{rec}}(f) = H_f \otimes (L_f \circ \pairMonad_{\mathsf{rec}}(L_f))$）已被构造。Lilies 选择公理 3，因此显式拒绝 $P^2 \not\cong P$ 的工程模式。
- **替代方案**：`subagent_spawn`（有边界子 Agent，非递归嵌套）、`cluster_publish/subscribe`（对等 Agent 协作）。

### ADR-003：拒绝 "让每个积木都内置 LLM"

- **违反定理**：Thm 3.2（自由构造定理）
- **形式理由**：$\pairMonad(\mathrm{level}_0) = \mathrm{level}_1$，不是反过来。在 level_0（积木级）重建 Pair 复合体违反了层次分离。每个内置 LLM 的积木 = 在更细粒度上重建 $P$ 结构，而 $P^2 \cong P$ 意味着这不会产生新的表达能力。
- **替代方案**：积木保持"傻"（确定性），组合保持"聪明"（通过 Builder 和 Template 引入 LLM 策略）。

### ADR-004：拒绝 "新通信原语"（broadcast/rpc/shared_memory 作为 block）

- **违反定理**：Thm 8.4（L1 完全性定理）
- **形式理由**：所有并发安全协议可归约到 L1 的 4 积木。broadcast、rpc、shared_memory 均可由 publish/subscribe 组合表达。如果新原语不能独立于 {P,S,A,R} 定义（即其语义可由 L1 积木的组合模拟），则不应该作为独立的 block 注册。
- **替代方案**：将 broadcast/rpc/shared_memory 作为 **Template** 实现，而非 block。仅在 Builder 搜索效率需要时注册为 derives_from 便利积木。

---

## 8. 跨阶段不变量

无论阶段二如何实现以下具体选择，以下不变量必须保持成立：

```
INV-1 (L1 完全性):
  所有并发协调模式的实现必须可归约到 {publish, subscribe, acquire, release} 4 积木。
  新原语若不可归约，必须提供形式证明。

INV-2 (不动点):
  架构层级数 ≤ 3 (block → workflow → cluster)。
  任何横向扩展必须保持 level_2 结构。
  不得引入 level_3+ 结构。

INV-3 (Det 闭包):
  确定性操作在复合、张量积和 trace 下的闭包性质保持不变。
  乐观并发的安全边界由 Det 闭包定理精确定义。

INV-4 (Pair 结构):
  任何引入 LLM 的组件必须保持 Harness(确定性) + LLM(非确定性) 的分离。
  LLM 输出不可直接作为 Harness 配置。Harness 配置不可依赖 LLM 的"正确理解"。

INV-5 (伴随关系):
  跨工作流通信必须保持 publish ⊣ subscribe 的伴随结构。
  新通信模式必须是伴随对的组合，不能破坏伴随同构。

INV-6 (遥测完整性):
  每个交互事件必须携带 Lamport 时钟、agent_id、topic/resource_id。
  每个运行必须可通过遥测数据完全重放交互序列。
```

---

## 9. 参考文件索引

### 范畴论文档
- `docs/source-materials/2026-07_initial_architecture_research/the_pair_categorical.tex` — 20 个定理的权威形式化

### 智力资产
- `docs/intellectual-assets/asset_the_pair_formal_system.md` — The Pair 形式系统（FS 版本）
- `docs/intellectual-assets/asset_cluster_minimality_proof.md` — L0/L1/L2 最小谱系的严格证明
- `docs/intellectual-assets/asset_harness_llm_composite.md` — Harness+LLM 原子性
- `docs/intellectual-assets/asset_theoretical_review.md` — 六条外部理论的 Lilies 映射

### 阶段文档
- `docs/workingon/plan_categorical_theory_driven_engineering_v1.md` — 阶段一交付与实证数据
- `docs/workingon/plan_cluster_messaging_v1.md` — 集群通信基础设施（L2 实现完成）

### 代码
- `platform/backend/src/agent_platform/cluster_messaging.py` — 783 行，L1 完备性原语
- `platform/backend/src/agent_platform/cluster_blocks.py` — 245 行，6 个集群积木
- `platform/backend/src/agent_platform/cluster_telemetry.py` — 377 行，遥测系统
- `platform/backend/src/agent_platform/cluster_runner.py` — 576 行，场景运行器
- `platform/backend/src/agent_platform/cluster_analysis.py` — 485 行，模式发现引擎
- `examples/cluster_task_market.py` — 663 行，Task Market 场景实例
- `tests/test_cluster_l1.py` — 818 行，25 个集成测试

---

*The Pair is a monad. Workflows are its algebras. Clusters are its fixed point. Transport is a functor. Everything else is Yoneda.*
