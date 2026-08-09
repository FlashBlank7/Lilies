# plan_v1_theory_completion_stage

## 1. 状态：📋 待执行

本计划合并两份设计文档的全部可执行任务：

- `docs/current-design/design_cluster_limitations_and_categorical_solutions_v1.md` — 六个硬限制的范畴解决方案（L1-L6）
- `docs/current-design/design_theory_mandated_engineering_gaps_v1.md` — 定理-工程缺口（G1-G7）

**合并理由**：任务 L3/L4 与任务 G1/G3 共享修改文件（`cluster_messaging.py`, `blocks.py`, `workflow_runtime.py`），并行实施可避免合并冲突。总计约 800 行增量代码 + 3 个文档。

## 2. 设计依据

- `docs/source-materials/2026-07_initial_architecture_research/the_pair_categorical.tex` — 20 个定理
- `docs/intellectual-assets/asset_cluster_minimality_proof.md` — L0/L1/L2 谱系
- `docs/current-design/design_cluster_limitations_and_categorical_solutions_v1.md` — 六硬限制
- `docs/current-design/design_theory_mandated_engineering_gaps_v1.md` — 七工程缺口
- `docs/workingon/plan_categorical_theory_driven_engineering_v1.md` — 已完成工程（基线）

## 3. 范围

**包含（P0，本阶段立即实施）**：

| ID | 来源 | 内容 | 文件 | 行数 |
|----|------|------|------|------|
| L3 | 限制 | 复合主键 schema 迁移 + presheaf 还原（读-读兼容） | `cluster_messaging.py` | ~50 |
| L4 | 限制 | 消息 TTL 自动清理（过期自然变换） | `cluster_messaging.py` | ~20 |
| G1 | 缺口 | `BlockRegistry.is_deterministic()` + determinism analysis pass | `blocks.py` | ~100 |
| G3 | 缺口 | 嵌套 loop 检测 + 紧致性优化 warning | `workflow_runtime.py` | ~130 |

**包含（P1，本阶段完成后立即启动）**：

| ID | 来源 | 内容 | 文件 | 行数 |
|----|------|------|------|------|
| L5 | 限制 | ACL 认证/授权（子对象分类器） | `cluster_messaging.py` | ~50 |
| G1b | 缺口 | Det-only 子图自动锁省略 | `workflow_runtime.py` | ~60 |
| G4 | 缺口 | 积木分类标注 + 新积木判定流程文档 | `blocks.py` + 新建文档 | ~110 + 文档 |
| G2 | 缺口 | Kleisli compose / specialize / diff | `merge_engine.py` | ~200 |
| G5 | 缺口 | 4 个 ADR | 新建 `adr_categorical_constraints.md` | 文档 |

**不包含（P2-P3，留待后续）**：

| ID | 内容 | 原因 |
|----|------|------|
| L1+L2 | 传输后端抽象（Redis/NATS/Kafka） | 需要先评估后端选型和运维影响 |
| L6 | 分布式共识（Raft/Redlock） | 依赖 L1 传输后端先就位 |
| G6 | Yoneda presheaf 完备性检查 | 概念层面需先明确 Env 范畴建模 |
| G7 | 跨平台翻译函子 | 远期研究 |

## 4. 关键决策

### 4.1 Schema 迁移策略

L3（锁粒度）涉及 `cluster_locks` 表的主键变更。选择**渐进式迁移**：

1. 新建 `cluster_locks_v2` 表（复合主键 `(resource_id, owner_id)`）
2. 从旧表迁移数据（每条记录保持，owner_id 即旧主键）
3. `ConflictDetector` 方法改为读写新表
4. 旧表保留一周后删除

**理由**：避免在生产数据库上直接 DROP TABLE，防止数据丢失。

### 4.2 确定性判定白名单

G1 的 `BlockRegistry.is_deterministic()` 基于**积木类型白名单**：

```python
DET_WHITELIST = {
    "start", "end", "schedule_trigger",
    "if_else", "loop",
    "template_transform", "variable_assigner", "variable_aggregator",
    "task_dispatcher",
}
```

白名单的维护原则：积木的所有执行路径不含 LLM 调用 AND 不依赖外部非确定性 I/O。此名单由 G4（积木分类体系）正式化后的判定流程治理。

### 4.3 Loop 优化策略

G3 的 loop 公理验证采用**非侵入式 warning** 策略：

- 在 `validate_workflow` 中检测嵌套 loop → 发出 `LoopNestingWarning`
- 在 `validate_workflow` 中检测 loop 体的前置 DAG 段 → 建议 `TighteningOpportunity`
- 在 `validate_workflow` 中检测纯状态传递 loop → 建议 `YankingSimplification`

**不自动执行优化**：自动展平嵌套 loop（Vanishing）会改变工作流的运行时行为（如 checkpoint 位置、错误传播路径）。保持语义显式，提供 Builder 建议。

### 4.4 ACL 默认策略

L5（认证/授权）采用**默认开放 + 可选锁定**策略：

- ACL 表为空 → 全部允许（向后兼容，零配置即可运行 Task Market）
- ACL 表有记录 → 仅允许匹配规则的 Agent 操作对应 topic
- 生产部署时通过环境变量 `LILIES_CLUSTER_ACL_ENFORCE=1` 启用强制模式

## 5. 实现路径

### P0 — 第一阶段（~300 行代码）

#### 步骤 1：L3 锁粒度修复

**文件**：`cluster_messaging.py`

1. Schema 迁移：新增 `cluster_locks_v2` 表，`ConflictDetector.__init__` 中检测并迁移
2. `acquire` 重写：条件检查 + 读-读兼容逻辑（按照 Thm 8.4 的完整语义）
3. `release`、`refresh`、`lock_holders`、`upgrade_lock` 适配新 schema
4. 更新测试：`test_lock_upgrade_blocked_by_other_readers` 恢复为多读者场景

#### 步骤 2：L4 消息 TTL

**文件**：`cluster_messaging.py`

1. `ClusterMessageBus.__init__` 增加 `ttl_seconds: float | None = None` 参数
2. 新增 `compact(ttl_seconds)` 方法
3. 新增 `_start_compaction_task()` 后台协程（周期性执行）
4. `_fetch_next` 增加 cursor 安全跳转（跳过已过期消息）

#### 步骤 3：G1 Det 闭包

**文件**：`blocks.py`

1. `BlockRegistry` 新增 `is_deterministic(block_type) -> bool`
2. `BlockRegistry` 新增 `_analyze_determinism(workflow) -> dict[str, bool]`
3. 在 `validate_workflow` 输出中增加 `determinism_map`
4. 测试：确定性工作流全标记 `deterministic`，含 LLM 工作流标记传播

#### 步骤 4：G3 Trace 公理

**文件**：`workflow_runtime.py`

1. 新增 `_detect_nested_loops(workflow) -> list[LoopNestingWarning]`
2. 新增 `_detect_tightening_opportunities(loop_node) -> list[TighteningHint]`
3. 新增 `_detect_yanking_opportunities(loop_node) -> list[YankingHint]`
4. 在 `validate_workflow` 中集成上述检测，输出 warning 列表

### P1 — 第二阶段（~420 行代码 + 文档）

#### 步骤 5：L5 ACL

**文件**：`cluster_messaging.py`

1. 新增 ACL 表 schema
2. 新增 `grant(agent_id, topic_pattern, permissions)`, `revoke(agent_id, topic_pattern)`
3. 新增 `_check_acl(agent_id, topic, perm) -> bool`
4. `publish`, `subscribe`, `conditional_publish` 入口增加 ACL 检查

#### 步骤 6：G1b 自动锁省略

**文件**：`workflow_runtime.py`

1. 在集群积木执行路径中，利用 G1 的 `determinism_map` 判定临界区
2. Det-only 临界区 → 跳过 `cluster_acquire`/`cluster_release`
3. 输出 info log + 可配置开关 `force_lock=True` 覆盖

#### 步骤 7：G4 积木分类

**文件**：`blocks.py` + 新建 `docs/current-design/design_block_classification_v1.md`

1. `BlockDefinition` 新增 `derivation: Literal["generator", "convenience"]` 字段
2. 对已有 51 个积木做分类标注
3. 新建判定流程文档

#### 步骤 8：G2 Kleisli 操作

**文件**：`merge_engine.py`

1. 新增 `compose_templates(template_a, template_b) -> Template`
2. 新增 `specialize_template(template, context) -> Template`
3. 新增 `diff_templates(template_a, template_b) -> Patch`
4. 新增 `apply_patch(template, patch) -> Template`

#### 步骤 9：G5 ADR

**文件**：新建 `docs/current-design/adr_categorical_constraints.md`

记录 4 条架构决策：拒绝 level_4、拒绝递归 Agent、拒绝积木内置 LLM、拒绝新通信原语。

## 6. 依赖设计

```
the_pair_categorical.tex
    │
    ├── Thm 8.3 (Det 闭包) ──────→ G1: determinism pass (blocks.py)
    │                               G1b: lock omission (workflow_runtime.py)
    │
    ├── Thm 3.3 (Trace) ─────────→ G3: loop validation (workflow_runtime.py)
    │
    ├── Thm 8.4 (L1 完全性) ─────→ L3: presheaf 还原 (cluster_messaging.py)
    │
    ├── 自然变换 ─────────────────→ L4: TTL compact (cluster_messaging.py)
    │
    ├── 子对象分类器 ─────────────→ L5: ACL (cluster_messaging.py)
    │
    ├── Thm 5.2 (Kleisli) ───────→ G2: compose/specialize/diff (merge_engine.py)
    │
    ├── Thm 3.2 (自由构造) ───────→ G4: block classification (blocks.py + doc)
    │
    └── Thm 7.1, 8.5, 3.2, 6.1 ─→ G5: ADRs (new document)
```

## 7. 验收标准

### 功能验收

**P0**：
- [ ] `cluster_locks` 使用复合主键 `(resource_id, owner_id)`
- [ ] 读-读兼容：同一资源可被多个 Agent 同时持有读锁
- [ ] 写锁仍互斥：有写锁时其他 Agent 无法获取任何锁
- [ ] 锁升级：仅当唯一持有者时可 read→write
- [ ] 消息 TTL：`compact(ttl)` 删除超期消息，订阅者 cursor 安全跳转
- [ ] `BlockRegistry.is_deterministic("llm") == False`
- [ ] `BlockRegistry.is_deterministic("template_transform") == True`
- [ ] determinism map 沿 DAG 正确传播（非确定节点污染下游）
- [ ] 嵌套 loop 检测 + 紧致性 warning + Yanking 检测

**P1**：
- [ ] ACL 表支持 agent+topic pattern 匹配
- [ ] `publish`/`subscribe` 在 ACL 非空时执行权限检查
- [ ] ACL 为空时完全向后兼容
- [ ] Det-only 临界区自动跳过锁（可配置）
- [ ] `BlockDefinition.derivation` 字段存在，已有积木全部分类
- [ ] `compose`/`specialize`/`diff`/`apply_patch` 可用
- [ ] `adr_categorical_constraints.md` 完成

### 非回归验收

- [ ] 全量已有测试（含 25 个集群测试）无回归
- [ ] Lint clean on all changed files
- [ ] Schema 迁移在空数据库和已有数据数据库上均正确执行
- [ ] Task Market 场景在 L3 修复后冲突率下降（读-读兼容生效）

### 理论一致性验收

- [ ] L3 实现匹配 Thm 8.4 的完整 Lock presheaf 语义
- [ ] L4 实现满足 $\mathrm{expire}_{\Delta t}$ 的四条保结构性
- [ ] L5 实现保持 publish ⊣ subscribe 的受限伴随
- [ ] G1 实现覆盖 Det 闭包定理的三个闭包性质（∘, ⊗, Tr）
- [ ] G3 实现覆盖 Joyal-Street-Verity 四条 Trace 公理中的三条（Tightening, Vanishing, Yanking）

## 8. 相关文件清单

| 文件 | 变更类型 | 估计行数 |
|------|---------|---------|
| `cluster_messaging.py` | 修改 | +200 |
| `blocks.py` | 修改 | +140 |
| `workflow_runtime.py` | 修改 | +190 |
| `merge_engine.py` | 修改 | +200 |
| `test_cluster_l1.py` | 修改 | +50（新增测试） |
| `docs/current-design/design_block_classification_v1.md` | 新建 | 文档 |
| `docs/current-design/adr_categorical_constraints.md` | 新建 | 文档 |
| **总计** | | **~780 行代码 + 2 份文档** |
