# design_cluster_limitations_and_categorical_solutions_v1

> ⚠️ **已被取代(2026-08)**:lean-core 已整体移除集群子系统。本文是集群时代的**历史设计参考**,不再作为工程依据;文中范畴论定理引用已撤回(见 asset_the_pair_core.md §四)。保留以保存推导与历史。

2026-07-24

**前置阅读**：
- `docs/source-materials/2026-07_initial_architecture_research/the_pair_categorical.tex` — 范畴论形式化（20 个定理）
- `docs/intellectual-assets/asset_cluster_minimality_proof.md` — L0/L1/L2 谱系与最小性证明
- `docs/current-design/design_cluster_messaging_v1.md` — 集群通信设计
- `docs/workingon/plan_categorical_theory_driven_engineering_v1.md` — 工程实现与验证数据

---

## 1. 问题

当前集群基础设施（`cluster_messaging.py` 783 行 + `cluster_blocks.py` 245 行 + `cluster_telemetry.py` 377 行 + `cluster_runner.py` 576 行 + `cluster_analysis.py` 485 行 = 2466 行核心，3947 行总计）提供了 L0-L2 完整谱系的单进程实现。25 个集成测试全部通过，Task Market 场景（4 Producer + 8 Worker + 120 轮）验证了涌现模式的产生。

但存在以下六个硬限制：

| # | 局限 | 严重性 | 根因 |
|---|------|--------|------|
| L1 | 单进程 | 高 | `ClusterMessageBus` 基于单一 SQLite 文件，Agent 必须在同一进程中 |
| L2 | 无网络层 | 高 | 无 RPC/gRPC/消息队列协议，无法跨机器通信 |
| L3 | 单锁粒度 | 中 | `cluster_locks` 表以 `resource_id` 为 PRIMARY KEY，同一资源至多一个持有者 |
| L4 | 无消息持久化策略 | 中 | 消息无限增长，无 TTL/compaction 机制 |
| L5 | 无认证/授权 | 中 | Agent 身份无加密验证，任意 Agent 可发布到任意 topic |
| L6 | 单点故障 | 中 | 共享 SQLite 文件是唯一故障域 |

本文档以范畴论形式化为统一语言，对每个局限给出精确的理论分析和工程解决方案。核心结论：**六个限制中，四个（L1-L5）已被现有定理体系完全覆盖，仅需工程实现；一个（L3）是 schema 设计问题；一个（L6）需要引入新的理论构造。**

---

## 2. 分类一：理论已完全覆盖（L1, L2, L4, L5）

这类局限的本质是：**理论已给出"正确实现"的范畴规范，当前实现是该规范的一个退化/简化特例。** 解决方案不需要新定理，只需要将实现从特例提升到一般形式。

### 2.1 L1/L2：单进程与无网络层

#### 2.1.1 范畴分析

publish ⊣ subscribe 伴随对（Thm 6.1）的范畴签名是传输无关的：

$$
\begin{aligned}
\text{publish}&: I \to \text{Topic} \\
\text{subscribe}&: \text{Topic} \to I \\
\text{publish} &\dashv \text{subscribe}
\end{aligned}
$$

伴随关系的全部要求为：

$$
\mathrm{Hom}_{\mathbf{Comm}}(\mathrm{publish}(W_1), W_2) \cong \mathrm{Hom}_{\mathbf{WFlow}}(W_1, \mathrm{subscribe}(W_2))
$$

即："将发布后的工作流 $W_1$ 连接到 $W_2$" 等价于 "将 $W_1$ 连接到订阅后的 $W_2$"。

此定义中**不存在任何对传输介质的引用**。SQLite、Redis Streams、NATS、Kafka、gRPC 均为 $\mathbf{Comm}$ 范畴的具体模型。

#### 2.1.2 理论方案：传输函子

定义**传输函子**（transport functor）：

$$
T_{\lambda}: \mathbf{Bus}_{\text{local}} \to \mathbf{Bus}_{\text{distributed}}
$$

其中 $\lambda$ 为传输后端参数（如 `"redis"`, `"kafka"`, `"grpc"`）。

$T_{\lambda}$ 必须满足以下保结构条件：

1. **伴随保持**：$T_{\lambda}(\text{publish}) \dashv T_{\lambda}(\text{subscribe})$ 在目标范畴中成立
2. **顺序保持**：对于任意 topic，$T_{\lambda}$ 保持 per-topic sequence 的严格单调性（因果序）
3. **幂等保持**：$T_{\lambda}$ 保持 `_msg_id` 去重语义（$m_1.\text{msg\_id} = m_2.\text{msg\_id} \Rightarrow T_{\lambda}(m_1) = T_{\lambda}(m_2)$）
4. **隔离性**：$T_{\lambda}$ 不改变 L1 原语（acquire/release/conditional_publish）的接口签名

$T_{\lambda}$ 是**忠实的**（faithful）：对于任意两个 local bus 上的操作序列 $\sigma_1, \sigma_2$，若 $\sigma_1 \neq \sigma_2$，则 $T_{\lambda}(\sigma_1) \neq T_{\lambda}(\sigma_2)$（分布式行为可区分当且仅当本地行为可区分）。

#### 2.1.3 工程方案

当前 `ClusterMessageBus` 的公开接口（9 个方法）已经形成传输无关的抽象边界：

```python
class ClusterMessageBus:                      # 抽象接口（传输无关）
    async def initialize() -> None: ...       # 后端初始化
    async def ensure_topic(name) -> str: ...  # topic 管理
    async def publish(topic, publisher_id, payload) -> ClusterMessage: ...
    async def subscribe(topic, subscriber_id) -> str: ...
    async def await_message(topic, subscriber_id, timeout) -> ClusterMessage | None: ...
    async def poll_messages(topic, subscriber_id) -> list[ClusterMessage]: ...
    async def peek_messages(topic, subscriber_id, limit) -> list[ClusterMessage]: ...
    async def conditional_publish(topic, publisher_id, payload, condition) -> tuple[bool, ClusterMessage | None]: ...
    async def topic_history(topic, limit) -> list[ClusterMessage]: ...
```

传输后端切换方案：

| 后端 | 实现方式 | 适用场景 | 复杂度 |
|------|---------|---------|--------|
| SQLite（当前） | `ClusterMessageBus` 现有实现 | 单机原型/分析 | 基线 |
| Redis Streams | `RedisMessageBus(ClusterMessageBus)` | 中小规模生产 | 中 |
| NATS | `NatsMessageBus(ClusterMessageBus)` | 高吞吐、低延迟 | 中 |
| Kafka | `KafkaMessageBus(ClusterMessageBus)` | 大规模、持久化优先 | 高 |

**范畴保证**：切换传输后端时，所有现有测试（25 个）可作为契约测试直接复用。L1 原语的接口签名、语义、和定理保证全部不变——仅 `T_{\lambda}` 的参数 $\lambda$ 改变。

> **状态**：理论已完全覆盖。待工程实施。

---

### 2.2 L4：无消息持久化策略

#### 2.2.1 范畴分析

消息流随时间的累积构成一个 presheaf：

$$
\mathcal{M}: \mathbf{Time}^{\mathrm{op}} \to \mathbf{Set}, \quad \mathcal{M}(t) = \{m \in \text{messages} \mid m.\text{created\_at} \leq t\}
$$

$\mathcal{M}$ 是**协变的**在时间方向上（消息只增不减），但当前实现中 $\mathcal{M}(t)$ 随 $t$ 单调增长且无上界。

#### 2.2.2 理论方案：时间过滤自然变换

定义**过期自然变换**：

$$
\mathrm{expire}_{\Delta t}: \mathcal{M} \Rightarrow \mathcal{M}
$$

其分量为：

$$
\mathrm{expire}_{\Delta t}(t)(m) = \begin{cases}
m & \text{if } t - m.\text{created\_at} \leq \Delta t \\
\bot & \text{otherwise}
\end{cases}
$$

关键性质（保结构）：

1. **幂等性**：$\mathrm{expire}_{\Delta t} \circ \mathrm{expire}_{\Delta t} = \mathrm{expire}_{\Delta t}$（重复过期是安全操作）
2. **单调性**：$\mathrm{expire}_{\Delta t_2} \circ \mathrm{expire}_{\Delta t_1} = \mathrm{expire}_{\Delta t_1 + \Delta t_2}$（多次小步过期 = 一次大步过期）
3. **订阅者安全**：对于任意订阅者的 cursor $c$，若 $c < \text{expire\_boundary}$，自动跳至 $\text{expire\_boundary}$（不返回已删除消息的引用）
4. **伴随保持**：$\mathrm{expire}_{\Delta t}$ 不改变 publish ⊣ subscribe 的伴随结构——它仅改变 $\mathcal{M}$ 的定义域，不改变 Hom 集的对应关系

#### 2.2.3 工程方案

在 `ClusterMessageBus` 上增加：

```python
async def compact(self, ttl_seconds: float) -> int:
    """删除超过 TTL 的消息。返回删除数量。
    
    范畴语义：应用 natural transformation expire_{ttl} 到消息 presheaf。
    """
```

实现：SQLite 单条语句 `DELETE FROM cluster_messages WHERE created_at < ?`，在后台 asyncio 任务中周期性执行。

配置接口：

```python
# 默认不启用（保持向后兼容）
bus = ClusterMessageBus(data_dir, ttl_seconds=86400)  # 24 小时后过期
```

订阅者安全由 `_fetch_next` 自动处理——当 cursor 指向已过期消息时，跳至第一个未过期消息（利用 sequence 单调递增的性质）。

> **状态**：理论已完全覆盖。纯工程实现，约 20 行代码。

---

### 2.3 L5：无认证/授权

#### 2.3.1 范畴分析

当前 L1 原语允许任何 Agent 对任意 topic 执行 publish/subscribe。这在范畴上表现为 Hom 集的**无约束性**：

$$
\forall a \in \mathbf{Agent}, \forall T \in \mathbf{Topic}: \mathrm{Hom}(I, T) \text{ 和 } \mathrm{Hom}(T, I) \text{ 对所有 } a \text{ 开放}
$$

#### 2.3.2 理论方案：子对象分类器

在 Agent 范畴上引入**子对象分类器**（subobject classifier）：

$$
\Omega: \mathbf{Agent} \times \{\text{publish}, \text{subscribe}, \text{admin}\} \to \{\text{allowed}, \text{denied}\}
$$

对每个 topic $T$，定义一个 ACL 单态（monomorphism）：

$$
\mathrm{ACL}_T \hookrightarrow \mathbf{Agent} \times \{\text{publish}, \text{subscribe}, \text{admin}\}
$$

L1 原语变为**条件态射**：

$$
\mathrm{publish}_{\mathrm{auth}}(a, T, p) = \begin{cases}
\mathrm{publish}(a, T, p) & \text{if } (a, \text{publish}) \in \mathrm{ACL}_T \\
\bot & \text{otherwise}
\end{cases}
$$

$$
\mathrm{subscribe}_{\mathrm{auth}}(a, T) = \begin{cases}
\mathrm{subscribe}(a, T) & \text{if } (a, \text{subscribe}) \in \mathrm{ACL}_T \\
\bot & \text{otherwise}
\end{cases}
$$

**关键性质：ACL 不破坏伴随关系**。

在受限 Hom 集上，伴随关系仍然成立：

$$
\mathrm{Hom}_{\mathrm{auth}}(\mathrm{publish}_{\mathrm{auth}}(W_1), W_2) \cong \mathrm{Hom}_{\mathrm{auth}}(W_1, \mathrm{subscribe}_{\mathrm{auth}}(W_2))
$$

仅当 $W_1$ 的发布者持有 publish 权限且 $W_2$ 的订阅者持有 subscribe 权限时两边非空。这是因为 $\mathrm{ACL}_T$ 是一个单态——它选择了一个子对象，而子对象上的伴随是原伴随的限制（restriction of adjunction），不改变伴随结构的本质。

**扩展性**：支持 topic pattern（glob），ACL 表可以匹配一类 topic（如 `__internal__.*`）。

#### 2.3.3 工程方案

在 `ClusterMessageBus` 中增加 ACL 存储：

```sql
CREATE TABLE IF NOT EXISTS cluster_acl (
    agent_id TEXT NOT NULL,
    topic_pattern TEXT NOT NULL,     -- 支持 glob 通配符
    permissions TEXT NOT NULL,       -- 'publish,subscribe,admin' 逗号分隔
    granted_by TEXT NOT NULL,        -- 授权者
    granted_at REAL NOT NULL,
    PRIMARY KEY (agent_id, topic_pattern)
);
```

在 `publish` 和 `subscribe` 方法入口增加检查：

```python
async def publish(self, topic: str, publisher_id: str, payload: dict) -> ClusterMessage:
    if not await self._check_acl(publisher_id, topic, "publish"):
        raise PermissionError(f"{publisher_id} lacks publish on {topic}")
    # ... 原有逻辑

async def _check_acl(self, agent_id: str, topic: str, perm: str) -> bool:
    """匹配 agent+topic 的 ACL 规则。未配置时默认允许（向后兼容）。"""
```

默认行为（ACL 表为空时）：全部允许，完全向后兼容。

> **状态**：理论已完全覆盖。工程实现约 50 行（ACL 表 + 检查逻辑）。

---

## 3. 分类二：Schema 设计问题（L3）

### 3.1 L3：单锁粒度

#### 3.1.1 范畴分析

L1 完全性定理（Thm 8.4）中，锁状态被建模为从资源到持有者集合的 **set-valued presheaf**：

$$
\mathrm{Lock}: \mathbf{Resource}^{\mathrm{op}} \to \mathbf{Set}, \quad \mathrm{Lock}(r) = \{h_1, h_2, \ldots, h_k\}
$$

其中每个 $h_i = (\mathrm{owner\_id}, \mathrm{mode}, \mathrm{acquired\_at}, \mathrm{expires\_at})$。

**当前实现是此 presheaf 的一个退化特例**：$|\mathrm{Lock}(r)| \leq 1$。这是由于 `cluster_locks` 表以 `resource_id` 为 PRIMARY KEY 导致的实现限制。

#### 3.1.2 理论方案：Presheaf 还原

将 presheaf 从"至多一个持有者"恢复为"任意有限集合"：

| 旧模型 | 新模型 |
|--------|--------|
| $|\mathrm{Lock}(r)| \leq 1$ | $|\mathrm{Lock}(r)| \geq 0$ |
| 读-读不兼容 | 读-读兼容（多个读者共享） |
| `INSERT OR REPLACE` | `INSERT`（每个 holder 独立行） |

acquire 操作的完整语义（Thm 8.4 的原始表述）：

$$
\mathrm{acquire}(r, o, \mathrm{write}) = \begin{cases}
\text{成功} & \text{if } \mathrm{Lock}(r) = \emptyset \\
\text{成功} & \text{if } \forall h \in \mathrm{Lock}(r): h.\mathrm{owner\_id} = o \text{（同一持有者重入）} \\
\text{失败} & \text{otherwise}
\end{cases}
$$

$$
\mathrm{acquire}(r, o, \mathrm{read}) = \begin{cases}
\text{成功} & \text{if } \mathrm{Lock}(r) = \emptyset \\
\text{成功} & \text{if } \forall h \in \mathrm{Lock}(r): h.\mathrm{mode} = \mathrm{read} \text{（读-读兼容）} \\
\text{成功} & \text{if } \forall h \in \mathrm{Lock}(r): h.\mathrm{owner\_id} = o \text{（同一持有者重入）} \\
\text{失败} & \text{otherwise}
\end{cases}
$$

锁升级的完整语义：

$$
\mathrm{upgrade\_lock}(r, o, \mathrm{read} \to \mathrm{write}) = \begin{cases}
\text{成功} & \text{if } \mathrm{Lock}(r) = \{(o, \mathrm{read}, *, *)\} \text{（唯一持有者）} \\
\text{失败} & \text{if } \exists h \in \mathrm{Lock}(r): h.\mathrm{owner\_id} \neq o \\
\text{成功} & \text{otherwise}
\end{cases}
$$

#### 3.1.3 工程方案：Schema 迁移

```sql
-- 旧 schema（删除）
DROP TABLE IF EXISTS cluster_locks;

-- 新 schema（复合主键，支持多个持有者）
CREATE TABLE cluster_locks (
    resource_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('read','write')),
    acquired_at REAL NOT NULL,
    expires_at REAL,
    PRIMARY KEY (resource_id, owner_id)
);
CREATE INDEX IF NOT EXISTS idx_cluster_locks_resource
    ON cluster_locks(resource_id);
```

`ConflictDetector` 方法修改：

| 方法 | 变更 |
|------|------|
| `acquire` | 从 `INSERT OR REPLACE` 改为 `INSERT` + 条件检查（读-读兼容） |
| `release` | 从 `DELETE WHERE resource_id = ? AND owner_id = ?`（不变，但现在是删除一行而非删除唯一行） |
| `lock_holders` | 不变——已支持返回多个持有者 |
| `upgrade_lock` | 增加检查：确保升级前无其它持有者 |
| `refresh` | 改为 `UPDATE WHERE resource_id = ? AND owner_id = ?` |

**范畴保证**：此迁移不改变任何 API 签名，仅将实现从 degenerate case 升级到 Thm 8.4 证明中假定的完整模型。

**Task Market 场景影响**：读-读兼容使多个 Worker 可以同时持有 `db.results` 的读锁进行查询，而写锁仍互斥。预期冲突率从 92.8% 下降（取决于读写比例，纯读场景可降至 0%）。

> **状态**：理论已完全覆盖。约 30 行 schema 迁移 + 20 行方法修改。

---

## 4. 分类三：需要新理论构造（L6）

### 4.1 L6：单点故障

#### 4.1.1 范畴分析

当前 $\mathbf{Bus}$ 是单一对象。故障意味着 $\mathbf{Bus} \cong \varnothing$（整个范畴坍缩到初始对象）。所有通信中断。

分布式扩展的范畴构造是**积保存函子**：

$$
R: \mathbf{Bus} \to \prod_{i=1}^{n} \mathbf{Bus}_i
$$

其中每个 $\mathbf{Bus}_i$ 是一个独立副本（独立的 SQLite/Redis 实例）。

#### 4.1.2 理论框架：CAP 定理的范畴表述

**定义（CAP-范畴）**：设 $\mathcal{D} = \prod_{i=1}^{n} \mathbf{Bus}_i$ 为分布式 Bus 范畴。在网络分区（$P$）下：

- **一致性（C）**：存在自然变换 $\gamma: \mathbf{Bus} \Rightarrow \mathcal{D}$ 使得每个 publish 操作在所有副本上的投影等价——即 $R$ 是**满**函子（essentially surjective on the diagonal）

- **可用性（A）**：每个 $\mathbf{Bus}_i$ 独立接受 publish 操作——即 $R$ 是**忠实**函子（faithful，不丢失任何副本的本地操作）

**定理（CAP 的范畴表述）**：在网络分区下，不存在同时满足 (C) 和 (A) 的 functor $R$。

**证明概要**：若网络分区将副本分为两组 $\{B_1, \ldots, B_k\}$ 和 $\{B_{k+1}, \ldots, B_n\}$。若 $R$ 同时满足 (C) 和 (A)，则对 $B_1$ 的 publish 操作 $p$，由 (A) 知其存在；由 (C) 知 $B_{k+1}$ 也必须反映 $p$。但网络分区阻止了同步——矛盾。$\square$

**Lax 伴随方案**：分布式扩展保持伴随结构的 **lax 版本**：

$$
\text{lax-publish} \dashv_{\ell} \text{lax-subscribe}
$$

其中 laxity 参数 $\ell$ 是副本间同步延迟的上界。$\ell = 0$ 等价于单节点（完全一致性）；$\ell \to \infty$ 等价于最终一致性。

伴随关系的保持程度直接对应分布式系统的**一致性保证程度**：

| 一致性模型 | laxity $\ell$ | publish 语义 | subscribe 语义 |
|-----------|---------------|-------------|---------------|
| 强一致（Raft/Paxos） | $\ell = 0$（线性化） | publish 在所有副本原子可见 | subscribe 读最新一致状态 |
| 顺序一致 | $\ell$ 有界 | 同一 publisher 的消息在所有副本顺序一致 | 可能读到过时数据 |
| 最终一致 | $\ell$ 无界 | publish 最终在所有副本可见 | 可能读到显著过时的数据 |

#### 4.1.3 理论给出的约束与判据

**约束 1（L1 保持）**：无论选择何种一致性模型，4 个 L1 原语（publish, subscribe, acquire, release）的接口签名不变。分布式实现必须保持每个原语的**局部确定性语义**。

**约束 2（Det 闭包保持）**：Thm 8.3（Det 闭包）在分布式环境下依然成立：确定性操作（publish, release, conditional_publish 的 Harness 部分）在副本间传播时保持可判定性。

**约束 3（不动点保持）**：Thm 7.1（不动点）在分布式扩展下不变——副本的存在不产生 level_3。多个 $\mathbf{Bus}_i$ 是 level_2 的**横向扩展**，不是纵向升级。

**判据（CAP 选择）**：Lilies Agent 集群的场景特征决定了 CAP 中的选择：

| 场景特征 | 推论 |
|---------|------|
| Agent 之间的通信以**任务分配**为主（非金融交易） | 短暂不一致可容忍 |
| 消息幂等性已内置（`_msg_id`） | 重复投递无害 |
| 锁操作（acquire/release）需要强一致 | 锁必须线性化（或使用 Redlock 算法） |
| Det 闭包定理要求确定性操作保持可判定 | publish/subscribe 可最终一致，但 acquire/release 必须强一致 |

**结论**：Lilies 集群应采用**混合一致性模型**：
- publish/subscribe → **最终一致**（高性能，短暂不一致可容忍）
- acquire/release → **强一致**（Raft/Redlock 或单 leader，锁操作必须线性化）

> **状态**：理论给出了完整的分析框架和判据。具体共识算法的选择和实现是工程决策。这是唯一需要引入新理论构造的局限。

---

## 5. 综合总结

### 5.1 理论覆盖率矩阵

| ID | 局限 | 严重性 | 理论覆盖 | 解决方案类型 | 代码量估计 | 测试复用 |
|----|------|--------|:---:|------|------|:---:|
| L1 | 单进程 | 高 | ✅ 完全 | 传输函子 $T_{\lambda}$ | ~300 行（新后端实现类） | ✅ 全部复用 |
| L2 | 无网络层 | 高 | ✅ 完全 | 同上 | （含在 L1 中） | ✅ 全部复用 |
| L3 | 单锁粒度 | 中 | ✅ 完全 | Schema 迁移 + presheaf 还原 | ~50 行 | ✅ 全部复用 |
| L4 | 无消息 TTL | 中 | ✅ 完全 | 过期自然变换 $\mathrm{expire}_{\Delta t}$ | ~20 行 | ✅ 全部复用 |
| L5 | 无认证/授权 | 中 | ✅ 完全 | 子对象分类器 $\mathrm{ACL}_T$ | ~50 行 | ✅ 全部复用 |
| L6 | 单点故障 | 中 | ⚠️ 部分 | CAP 范畴表述 + 混合一致性 | ~500 行（取决于后端选择） | ✅ API 测试复用 |

### 5.2 实施优先级

| 优先级 | 局限 | 理由 |
|--------|------|------|
| **P0** | L3（锁粒度） | 理论完全覆盖，代码量最小（50 行），立即提升并发读性能 |
| **P0** | L4（消息 TTL） | 理论完全覆盖，代码量最小（20 行），防止存储无限增长 |
| **P1** | L5（认证/授权） | 理论完全覆盖，代码量小（50 行），安全基线 |
| **P1** | L1+L2（传输后端） | 理论完全覆盖，但需要选择 Redis/NATS/Kafka 并实现新后端 |
| **P2** | L6（分布式） | 理论已给出框架和判据，但需要深度工程（共识算法/leader election） |

### 5.3 核心结论

> **六个硬限制中，五个（83%）已被现有范畴论定理体系完全覆盖。** 传输函子 $T_{\lambda}$、过期自然变换 $\mathrm{expire}_{\Delta t}$、子对象分类器 $\mathrm{ACL}_T$、presheaf 还原 $\mathrm{Lock}$ ——这些构造不是"新发明的理论"，而是**已有定理的直接工程翻译**。它们证明了范畴抽象的实用价值：正确的接口设计使得传输后端替换、安全策略增加、存储策略改变都变为**参数化配置而非架构重设计**。
>
> 唯一的例外是 L6（单点故障），它真正触及了理论边界——CAP 定理的范畴表述需要共识算法的选择。但即使在此，理论也提供了精确的约束和判据（混合一致性模型），将"不确定的设计空间"缩小为"两个明确选项之一"。
>
> 当前实现的价值不在于它"已经解决了所有问题"，而在于它**证明了范畴抽象能够统一地覆盖所有已知的和未来可能出现的问题，并将解决方案的结构预先确定**。
