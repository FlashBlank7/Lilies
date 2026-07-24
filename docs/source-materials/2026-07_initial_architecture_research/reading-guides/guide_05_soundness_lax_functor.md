# Soundness 作为 Lax Functor：读懂 §5 的最小概念集

> 目标：能逐行读懂论文 Section 5（Soundness 作为 Lax Functor），理解验证函子 $\mathcal{S}$、确定性可判定、非确定性近似，以及 Lax/Strict/Oplax 三种 monoidal 结构的区别。

---

## 0. 大局观：§5 在干什么？

§4 讲模板怎么被记住。§5 讲一个更基本的问题：

> 工作流怎么被验证为"正确"？

```
§2 的遗产: 工作流 = DAG，soundness 的三个条件（可达终止、适当终止、无死任务）
                    ↓
§5 的回答: 验证是一个函子 S: WFlow → Bool
           - 对确定性工作流: strict monoidal（精确判定）
           - 对非确定性工作流: lax monoidal（最优近似）
```

---

## 1. 函子 $\mathcal{S}$：验证的数学抽象

### 1.1 定义

$$\mathcal{S}: \mathbf{WFlow} \to \mathbf{Bool}$$
$$\mathcal{S}(W) = \text{$W$ 的 soundness 布尔值}$$

其中 $\mathbf{Bool}$ 的 monoidal 结构是 $\land$（逻辑与）："两个工作流都 sound".

### 1.2 Soundness 的三个条件

（来自形式系统定义 10，对应 Petri 网的 WF-net soundness）：

1. **可达终止**：从任何可达状态都能到达 end
2. **适当终止**：到达 end 时，没有其他活跃节点
3. **无死任务**：每个节点在某个可达状态下可被执行

---

## 2. Lax / Strict / Oplax Monoidal Functor：关键区分

这是 §5 中最容易被混淆的概念。修正版论文已经理清了方向。

### 2.1 三种结构的方向

设 $F: \mathbf{C} \to \mathbf{D}$ 是两个 monoidal category 之间的函子。$F$ 保持 monoidal 结构有三种可能：

| 类型 | 结构映射方向 | 含义 |
|------|------------|------|
| **Lax** | $F(A) \otimes_D F(B) \to F(A \otimes_C B)$ | "如果两个分开的东西各自正确，那么合在一起也正确" |
| **Oplax** | $F(A \otimes_C B) \to F(A) \otimes_D F(B)$ | "如果合在一起的东西正确，那么两个分开的各自也正确" |
| **Strict** | 两者皆为同构（实际上是相等） | "分开正确 ⇔ 合在一起正确" |

### 2.2 在 Soundness 语境下的含义

代入 $\mathcal{S}: \mathbf{WFlow} \to \mathbf{Bool}$，$\mathbf{Bool}$ 的 monoidal 结构是 $\land$：

**Lax 方向**（非平凡）：
$$\mathcal{S}(W_1) \land \mathcal{S}(W_2) \to \mathcal{S}(W_1 \otimes W_2)$$

读作："如果 $W_1$ 和 $W_2$ 各自 sound，那么它们的并行组合也 sound。"

**为什么非平凡？** 两个各自"正确"的工作流放在一起，可能因为共享资源竞争而丧失 soundness。这个方向的成立需要证明——（对于 DAG 工作流来说是成立的）。

**Oplax 方向**（平凡）：
$$\mathcal{S}(W_1 \otimes W_2) \to \mathcal{S}(W_1) \land \mathcal{S}(W_2)$$

读作："如果并行的两个工作流 sound，那么各自也 sound。" 这因分量独立而平凡成立。

### 2.3 确定性与非确定性的区别

| 子范畴 | 结构类型 | 理由 |
|--------|---------|------|
| $\mathbf{WFlow}_{\mathrm{DAG}}$（确定性） | **Strict** | 两个方向都成立——并行不引入交互 |
| $\mathbf{WFlow}$（含 LLM） | **Lax**（非平凡方向） | Lax 方向需要验证，oplax 方向平凡 |

---

## 3. 确定性 Soundness 可判定

### 3.1 定理

> 定理 5.1（确定性 Soundness 可判定）：$\mathcal{S}$ 在 $\mathbf{WFlow}_{\mathrm{DAG}}$ 上的限制是 strict monoidal functor。

### 3.2 直觉

对于不含 LLM 的纯 DAG 工作流，soundness 的三个条件可以通过**结构检查**在多项式时间内判定：

1. 可达终止：从 start BFS → 所有节点可达 + 所有路径可达 end
2. 适当终止：拓扑排序验证 end 是唯一 sink
3. 无死任务：BFS 验证从 start 可到达所有节点

**复杂度**：$O(|N| + |E|)$

### 3.3 Strictness 的证明

对于独立并行组合，一个子工作流的 soundness 不影响另一个。因此：
$$\mathcal{S}(W_1 \otimes W_2) = \mathcal{S}(W_1) \land \mathcal{S}(W_2)$$

Lax 结构映射和 oplax 映射互为逆。

### 3.4 Lilies 对应

```python
# Lilies 中的 validate_workflow() 就是这个判定
def validate_workflow(workflow):
    # 1. 可达终止：BFS 检查所有路径到 end
    # 2. 适当终止：验证 end 是唯一 sink
    # 3. 无死任务：BFS 检查所有节点可达
    return soundness_bool
```

---

## 4. 非确定性 Soundness 近似

### 4.1 为什么不能精确判定

含 LLM 的工作流的输出不可预测——LLM 可能产生不在预期路径上的输出（例如 tool 调用失败后的非预期分支）。

### 4.2 三层防御

论文指出，对于非确定性工作流，Lilies 采用三层策略：

| 条件 | 如何保证 | 完备性 |
|------|---------|--------|
| 无死任务（条件 3） | `validate_workflow` BFS 可达性 | ✅ 完备（纯结构） |
| 可达终止（条件 1） | `budget_gate` + `round_limit` 运行时防御 | ⚠️ 部分（LLM 非确定性） |
| 适当终止（条件 2） | `structural_only` 测试 | ❌ 不可静态保证 |

### 4.3 Lax natural transformation

> 定理 5.2（非确定性 Soundness 近似）：存在 lax natural transformation $\tau: \mathcal{S}_{\mathrm{approx}} \Rightarrow \mathcal{S}_{\mathrm{exact}}$。

**翻译**：工程近似（结构检查 + 运行时防御 + 测试降级）永远不能达到精确 soundness，但 $\tau$ 是最优近似——**没有更强的可静态判定的近似**。

---

## 5. 用论文串联

### §5.1: Soundness 函子

> 论文：定义 $\mathcal{S}$ → Lax 性说明 → Strictness 条件

**翻译**：验证是一个 lax monoidal functor。对于 DAG 工作流退化为 strict。

### §5.2: 确定性可判定

> 论文：定理 5.1（DAG 上的 strictness）→ 证明（三个条件 + 多项式时间）

**翻译**：纯 DAG 的 soundness 是精确可判定的。

### §5.3: 非确定性近似

> 论文：定理 5.2（lax natural transformation）→ 近似是最优的

**翻译**：含 LLM 的工作流，我们只能做到最优近似，不能做到精确判定。

---

## 6. 自检清单

在进入 §6 之前，确认你能回答：

- [ ] $\mathcal{S}(W)$ 的值域是什么？$\mathbf{Bool}$ 的 monoidal 结构是什么？
- [ ] Lax monoidal functor 的结构映射长什么样？方向是什么？
- [ ] Oplax 方向的直观含义是什么？为什么在 soundness 语境下它是平凡的？
- [ ] Strict 的条件是什么？DAG 子范畴为什么能满足？
- [ ] Soundness 三个条件各是什么？对于确定性工作流怎么判定？
- [ ] 为什么含 LLM 的工作流不能精确判定 soundness？
- [ ] Lilies 的三层防御策略各对应哪个 soundness 条件？
- [ ] $\tau: \mathcal{S}_{\mathrm{approx}} \Rightarrow \mathcal{S}_{\mathrm{exact}}$ 中的"最优"是什么意思？

---

## 7. 推荐阅读

| 材料 | 内容 | 预计时间 |
|------|------|---------|
| Mac Lane §XI.2 | Monoidal functor（lax/oplax/strict） | 1.5h |
| Van der Aalst (1998) §3 | WF-net soundness 的正式定义 | 2h |
| nLab: Lax Monoidal Functor | Lax 结构的形式定义 | 0.5h |
| 论文 §5 原文 | 结合本笔记精读 | 1h |

---

*这份笔记专门为读懂 `the_pair_categorical.tex` §5 编写。下一阶段（Guide 6）将覆盖 §6——集群扩展的 2-Categorical 结构：monoidal 2-category、publish ⊣ subscribe 伴随、锁不可消去证明、最小谱系 L0/L1/L2。*
