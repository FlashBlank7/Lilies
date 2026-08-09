# design_theory_mandated_engineering_gaps_v1

2026-07-24

**前置阅读**：
- `docs/source-materials/2026-07_initial_architecture_research/the_pair_categorical.tex` — 20 个定理
- `docs/current-design/design_cluster_limitations_and_categorical_solutions_v1.md` — 六个硬限制的范畴解决方案
- `docs/workingon/plan_categorical_theory_driven_engineering_v1.md` — 已完成的工程实现

---

## 1. 方法

遍历 `the_pair_categorical.tex` 的全部 20 个定理（含修订版新增 4 个），对每个定理执行以下判定：

1. 该定理是否给出了一个**可工程化的规范**？（是 / 否）
2. 该规范在当前代码中是否**已实现**？（是 / 否 / 部分）
3. 若未实现，缺失的具体内容是什么？

---

## 2. 定理-工程缺口矩阵

### 2.1 已完全工程化的定理（无缺口）

| # | 定理 | 定理内容 | 工程实现 | 文件 |
|---|------|---------|---------|------|
| 1 | Thm 1 (Pair Monad) | P 是幂等 monad | Harness+LLM 在各层的架构实例化 | 全栈 |
| 2 | Thm 3.4 ($ref functor) | Resolve: WFlow → Comp 是 faithful functor | `_resolve_refs()` + 拓扑排序执行 | `workflow_runtime.py` |
| 3 | Thm 5.1-5.2 (Soundness lax) | S: WFlow → Bool 是 lax monoidal functor | `validate_workflow()` DAG 检查 | `blocks.py` |
| 4 | Thm 6.1 (Cluster 2-morphism) | publish ⊣ subscribe | 6 个集群积木 + 3 个组件 | `cluster_messaging.py`, `cluster_blocks.py` |
| 5 | Thm 6.3 (Naturality) | η: F ⇒ G 是 natural transformation | cluster 积木不修改数据流语义 | `workflow_runtime.py` |
| 6 | Thm 7.1 (Fixed Point) | level_3 = level_2，两层迭代后饱和 | 无 level_3+ 结构产生 | `cluster_messaging.py` |
| 7 | Thm 8.2 (Retry Soundness) | Retry 不改变成功路径语义 | `RetryPolicy` + `ErrorStrategy` | `workflow_models.py` |
| 8 | Thm 8.5 (Axiom 3 Independence) | 反模型构造证明公理 3 独立 | `claude_agent` 标记为 legacy | `blocks.py` |

### 2.2 存在工程缺口的定理

---

#### 缺口 G1：Det 闭包定理 → 确定性的工程判据与优化

**定理**（Thm 8.3）：$\catDet$ 在顺序复合 $\circ$、张量积 $\otimes$ 和 trace $\mathrm{Tr}$ 下闭合。

**理论给定的规范**：

> 若一个工作流子图的全部节点属于 $\catDet$（不含 LLM 调用），则该子图的所有组合方式也属于 $\catDet$。$\catDet$ 中的态射可以安全并行执行，无需同步机制。

**当前工程状态**：`BlockRegistry` 未提供确定性判定接口。`validate_workflow` 不做 determinism analysis。

**缺失的具体实现**：

1. **确定性白名单查询**：`BlockRegistry.is_deterministic(block_type: str) -> bool`
2. **静态确定性传播**：对工作流 DAG 做前向传播——非确定性节点的所有下游节点标记为"受污染"
3. **并行安全优化**：纯 $\catDet$ 的并行分支自动省略锁获取
4. **确定性验证**：在 `validate_workflow` 中增加 `determinism_map` 输出

**复杂性**：O(|N| + |E|)，与现有 DAG 可达性检查同阶。

**测试**：确定性工作流 → 所有节点标记 `deterministic`；含 LLM 的工作流 → LLM 及其下游标记为 `non_deterministic`。

---

#### 缺口 G2：Kleisli 范畴 → 模板操作的完备化

**定理**（Thm 5.2）：模板市场的所有合法操作恰好是 $\monadT$ 的 Kleisli 范畴 $\mathrm{Kl}(\monadT)$ 中的态射。

**理论给定的规范**：

$\mathrm{Kl}(\monadT)$ 作为一个范畴，其态射复合和单位律定义了模板操作的**完备集合**。当前工程实现仅覆盖了此范畴的一个子集。

**当前工程状态**：`merge_engine.py` 实现了 `expand`、`merge` 和相似度计算。`template_strategy.py` 提供了策略选择。但 $\mathrm{Kl}(\monadT)$ 的结构还包含以下态射，它们均未实现：

| Kleisli 操作 | 范畴签名 | 语义 | 状态 |
|-------------|---------|------|:---:|
| `expand` | $A \to \monadT(A)$ | 将工作流注册为模板 | ✅ 已实现 |
| `publish` | $A \to \monadT(B)$ | 将工作流 A 发布为模板 B | ✅ 已实现（通过 Builder） |
| `merge` | $\monadT(A) \times A \to \monadT(A)$ | 合并新实例到已有模板 | ✅ 已实现 |
| `evolve` | $\monadT(A) \times A \to \monadT(A) + 1$ | 可能拒绝的进化 | ✅ 已实现 |
| **`compose`** | $\monadT(A) \times \monadT(B) \to \monadT(A \otimes B)$ | 两个模板的并行组合 | ❌ 未实现 |
| **`specialize`** | $\monadT(A) \times \mathrm{Context} \to \monadT(A')$ | 基于上下文的模板特化 | ❌ 未实现 |
| **`diff`** | $\monadT(A) \times \monadT(A) \to \mathrm{Patch}$ | 模板差异计算 | ❌ 未实现 |
| **`instantiate`** | $\monadT(A) \to A$ | 从模板生成具体工作流实例 | ⚠️ 部分（仅通过 Builder 间接实现） |

**缺失的具体实现**：

1. **`compose(T_A, T_B) -> Template`**：合并两个模板的节点集（ID 去重 + 前缀）和边集，构造并行组合模板
2. **`specialize(T, context) -> Template`**：对模板中的 `$ref` 和 `template_transform` 变量做上下文替换
3. **`diff(T_A, T_B) -> Patch`**：图同构检测 + 节点/边集合差，输出结构化 Patch
4. **`apply_patch(T, patch) -> Template`**：使 Patch 可回放

**复杂性**：`compose` 和 `specialize` 约 50 行/操作。`diff` 约 100 行（需要图同构检测）。

---

#### 缺口 G3：Trace 公理 → Loop 的语义验证与优化

**定理**（Thm 3.3）：$\catWFlow_{\mathrm{DAG}}$ 增加 trace（即 `loop` 积木）后成为 $\catWFlow_{\mathrm{traced}}$，满足 Joyal-Street-Verity 的 traced monoidal category 公理系统。

**理论给定的规范**：

| Trace 公理 | 公式 | 工程含义 |
|-----------|------|---------|
| **紧致性** (Tightening) | $\mathrm{Tr}(f \circ (g \otimes \id)) = \mathrm{Tr}(f) \circ g$ | loop 的前置操作可从 trace 中提出 |
| **消去性** (Vanishing) | $\mathrm{Tr}(\mathrm{Tr}(f)) = \mathrm{Tr}(f)$ | 嵌套 loop 等价于单层 loop |
| **超复合性** (Superposing) | $\mathrm{Tr}(f) \otimes g = \mathrm{Tr}(f \otimes g)$ | loop 可跨并行边界移动 |
| **Yanking** | $\mathrm{Tr}(\sigma_{X,X}) = \id_X$ | 简单状态回传不需要真实迭代 |

**当前工程状态**：`LoopConfig` 提供了丰富的配置参数（`break_condition`, `cancel_condition`, `max_iterations`, `state_input_name` 等），但**不验证**上述任何公理。

**缺失的具体实现**：

1. **嵌套 loop 检测与 warning**：检测 `loop.workflow` 中包含另一个 `loop` → 发出 Vaishing warning + 展平建议
2. **紧致性优化**：检测 loop 体中的前置 DAG 段是否与状态无关 → 将其从 trace 中提出，减少每次迭代的重复计算
3. **Yanking 检测**：检测 loop 体是否为纯状态传递（无 LLM、无外部调用）→ 建议用简单赋值替代 loop

**紧致性优化的具体代码效果**：

```
优化前（每轮迭代都重复执行前置操作）：
  loop {
    http_request("fetch_data")  ← 每轮都执行，但结果不变
    llm("process")
    state_update
  }

优化后（前置操作提出 trace）：
  http_request("fetch_data")   ← 只执行一次
  loop {
    llm("process")             ← 仅非确定性核心在循环内
    state_update
  }
```

**复杂性**：检测逻辑约 80 行（嵌套检测 + 前置 DAG 分析）。自动优化建议约 50 行。

---

#### 缺口 G4：积木分类体系 → 新积木增加的规范判据

**定理**（Thm 3.2）：由 CORE 的 6 个生成元 $\{\mathtt{start}, \mathtt{end}, \mathtt{llm}, \mathtt{if\_else}, \mathtt{loop}, \mathtt{template\_transform}\}$ 生成的自由严格 monoidal category $F(\mathrm{CORE})$ 等价于纯计算工作流子范畴 $\catWFlow_{\mathrm{comp}}$。

**理论给定的规范**：

$\catWFlow_{\mathrm{comp}}$ 排除了需要外部 I/O、通信基础设施、或并行语义的积木。这些被排除的积木需要在 CORE 之外作为额外的生成元加入。每个新积木要么属于 $F(\mathrm{CORE})$（可通过已有积木表达），要么是一个新的生成元。

**当前工程状态**：约 51 个积木在一个扁平的注册表中。没有"生成元 vs 便利积木"的分类元数据。

**缺失的具体实现**：

1. **积木分类标注**：为 `BlockDefinition` 增加 `derivation: "generator" | "convenience"` 字段
2. **便利积木的"源"标注**：若积木可由 CORE 生成元组合表达，标注其 canonical decomposition
3. **新积木提案的判定流程**：
   ```
   提出新积木 X →
     1. X 能用 CORE 的 6 个生成元 + 已有生成元组合表达吗？
        能 → X 是"便利积木" → 优先实现为 Template
        不能 → X 是"新生成元" →
           2. X 引入了什么新范畴结构？
              • 外部 I/O → 合理（如 http_request）
              • 跨工作流通信 → 合理（如 cluster_publish）
              • 并发安全 → 合理（如 cluster_acquire）
              • 人机交互 → 合理（如 human_input）
              • 其他 → 需要论证为什么不在 F(CORE) 的闭包内
   ```

4. **已有积木分类回顾**：

| 分类 | 积木示例 | 数量估计 |
|------|---------|---------|
| CORE 生成元 | start, end, llm, if_else, loop, template_transform | 6 |
| I/O 生成元 | http_request, web_collection, human_input, schedule_trigger | ~4 |
| 通信生成元 | cluster_publish, cluster_subscribe, cluster_register, cluster_discover, cluster_acquire, cluster_release | 6 |
| 便利积木（可归约到 CORE） | question_classifier (llm∘if_else), parameter_extractor (llm∘template_transform), collection_digest (template_transform), connector_action (http_request + 治理) | ~35 |

**复杂性**：新增字段约 10 行（`BlockDefinition` 增加 `derivation`）。判定流程文档约 100 行。已有积木分类回顾需要领域知识（约 2 小时人工分析）。

---

#### 缺口 G5：ADR 体系 → 四种被定理禁止的设计模式

以下四种设计提案被现有定理体系证伪。应正式化为架构决策记录（ADR）：

| ADR | 违反的定理 | 被拒绝的提案 | 被接受的替代方案 |
|-----|-----------|-------------|----------------|
| ADR-1 | 不动点定理 Thm 7.1 | "集群的集群"（level_3+） | 多 Bus 实例共享存储，或 topic 前缀路由 |
| ADR-2 | 公理 3 独立性 Thm 8.5 | 递归 Agent 嵌套（Agent 输出是 Agent） | `subagent_spawn`（有边界）+ `iteration`（同构） |
| ADR-3 | 自由构造 Thm 3.2 | "让每个积木内置 LLM 推理" | 积木保持"傻"，组合保持"聪明" |
| ADR-4 | 最小性 Thm 6.1 + Thm 8.4 | 新增 `broadcast`/`rpc`/`shared_memory` 通信原语 | 用 publish + subscribe 的 Template 表达 |

**缺失的具体实现**：

创建 `docs/current-design/adr_categorical_constraints.md`，为每个 ADR 记录：
- 提案描述
- 违反的定理及证明引用
- 为什么替代方案足够
- 复审条件（什么情况下应该重新考虑）

---

#### 缺口 G6：外部 Yoneda → 架构-环境交互的形式接口

**定理**（Thm 3.5 的扩展）：每个节点 $n$ 的 Yoneda embedding $\Yoneda(n): \catWFlow^{\mathrm{op}} \to \catSet$ 可扩展到整个架构：$\Yoneda(\mathrm{Arch}): \cat{Env}^{\mathrm{op}} \to \catSet$。

**理论给定的规范**：

> 架构由它的全部可能的环境交互方式**完全确定**。不存在"隐藏的架构本质"。

**工程含义**：

1. **交互完备性判据**：对于每个外部集成点（connector、HTTP endpoint、human_input 模式），必须验证其 Hom 集定义是否完备（类型签名、错误处理、超时、重试）
2. **测试完备性判据**：测试应覆盖 presheaf 空间的代表性采样点
3. **新 Connector 增加的判据**：新的外部系统交互模式是否与已有 presheaf 自然同构？若是，不需要新积木

**当前工程状态**：`connector_action`、`http_request`、`web_collection` 等提供了与外部交互的积木，但没有统一的"交互接口完备性"检查。

**缺失的具体实现**：

1. **Presheaf 完备性检查器**：对每个外部集成积木的配置做静态分析，检测是否覆盖了所有必需的交互模式
2. **Connector 生成判据**：新增 Connector 时，检查其 presheaf 是否与已有 Connector 的 presheaf 自然同构——若是，提示可归约

**复杂性**：概念层面，需要先明确 $\cat{Env}$ 的建模方式。工程实现可先做最简单的版本：验证每个外部集成积木的输入/输出端口是否完备。

---

#### 缺口 G7：自由构造的普遍性质 → 跨平台翻译函子

**定理**（Thm 7.2）：设 $\cat{C}$ 是任意满足公理 1-2（能力二分 + 不可约性）的范畴，则存在唯一的（在自然同构意义下）monoidal functor $U: \catWFlow \to \cat{C}$。

**理论给定的规范**：

> 若另一个团队也从相同的公理出发构建 Agent 工作流平台，他们将得到与 $\catWFlow_{\mathrm{comp}}$ 范畴等价的架构。

**工程含义**：

1. **工作流可移植性**：Lilies 的纯计算工作流（$\catWFlow_{\mathrm{comp}}$ 中的态射）有确定的翻译到其他满足公理的平台（如 Claude Code 的 hook 系统）
2. **平台迁移代价**：迁移的难点不在于结构化工作流（范畴等价保证可翻译），而在于平台特定的 Harness 实现（沙箱、权限、工具注册）
3. **跨平台验证**：两个平台上的"等价"工作流应该产生等价的计算结果

**当前工程状态**：未实现。这是一个较远期的工作方向。

**建议**：将此保留为远期研究方向。当前优先实现 G1-G5。

---

## 3. 缺口汇总与优先级

| ID | 定理 | 缺失内容 | 代码量 | 难度 | 优先级 |
|----|------|---------|--------|------|:---:|
| G1 | Thm 8.3 (Det 闭包) | determinism analysis pass + 自动锁省略 | ~100 行 | 低 | **P0** |
| G3 | Thm 3.3 (Trace 公理) | 嵌套 loop 检测 + 紧致性优化 | ~130 行 | 中 | **P0** |
| G1b | Thm 8.3 推论 | 并行安全自动判定 | ~60 行 | 低 | **P1** |
| G4 | Thm 3.2 (自由构造) | 积木分类体系 + 判定流程 | ~110 行 | 中 | **P1** |
| G2 | Thm 5.2 (Kleisli) | compose / specialize / diff | ~200 行 | 中 | **P1** |
| G5 | Thm 7.1, 8.5, 3.2, 6.1 | 4 个 ADR | 文档 | 低 | **P1** |
| G6 | Thm 3.5 扩展 (Yoneda) | presheaf 完备性检查 | ~150 行 | 高 | **P2** |
| G7 | Thm 7.2 (普遍性质) | 跨平台翻译函子 | 研究 | 高 | **P3** |

## 4. 与已有文档的关系

```
the_pair_categorical.tex (20 theorems)
    │
    ├── 已实现 (8 theorems) ─── 当前代码库
    │
    ├── 缺口 G1-G5 ─── 本文档 (5 个任务，~600 行代码)
    │   └── 可在 1-2 个 stage 内完成
    │
    ├── 缺口 G6-G7 ─── 远期研究
    │
    └── 六硬限制 ─── design_cluster_limitations_and_categorical_solutions_v1.md
        └── 独立推进，与 G1-G5 可并行
```

**总代码量估计**（G1-G5）：~600 行，分布在 4 个现有文件中（`blocks.py`, `merge_engine.py`, `workflow_runtime.py`）+ 2 个新文件（ADR 文档、判定流程文档）。
