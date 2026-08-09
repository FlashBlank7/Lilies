# 模板市场的 Monad 结构：读懂 §4 的最小概念集

> 目标：能逐行读懂论文 Section 4（模板市场的 Monad 结构），理解模板 Monad $T$、Kleisli 范畴 $\mathrm{Kl}(T)$、四个操作（expand/publish/merge/evolve）的范畴论表述，以及收敛定理。

---

## 0. 大局观：§4 在干什么？

§1-3 建立了能力、工作流组合、数据管道。§4 回答一个更高层次的问题：

> 好的工作流组合怎么被记住和复用？

```
§3 的遗产: $ref 是数据流管道，Resolve 函子保持结构
                    ↓
§4 的问题: 模板市场的操作 {expand, publish, merge, evolve}
           在数学上构成什么结构？
                    ↓
答案:    模板 Monad T
         T(W) = {W' | W' 从 W 通过 三操作 可达}
                    ↓
         Kl(T) = 模板市场的 Kleisli 范畴
         (所有合法操作恰好是 Kl(T) 中的态射)
                    ↓
         收敛定理: Kl(T) 中态射复合的余极限存在
         → "最终"模板集合
```

---

## 1. 模板 Monad $T$：知识闭包操作

### 1.1 直觉

给定一个工作流 $W$，你能从它出发演化出多少模板版本？

$$T(W) = \{W' \mid W' \text{ 从 } W \text{ 通过 } \{\mathrm{expand}, \mathrm{merge}, \mathrm{evolve}\} \text{ 可达}\}$$

**读作**：$T(W)$ 是 $W$ 的"知识闭包"——所有能从 $W$ 出发、通过合法操作到达的模板版本。

### 1.2 Monad 三要素

| 组件 | 符号 | 在模板市场中的含义 |
|------|------|------------------|
| 函子 | $T: \mathbf{WFlow} \to \mathbf{WFlow}$ | 模板闭包操作 |
| 单位 | $\eta_W: W \to T(W)$ | 把 $W$ 本身注册为模板（"当前知识就是模板的起点"） |
| 乘法 | $\mu_W: T^2(W) \to T(W)$ | 模板的模板退化为模板（"关于模板的知识仍是模板"） |

### 1.3 为什么是 monad

Monad 的编程直觉是"可组合的计算上下文"。这里的"上下文"是**模板版本空间**：每个操作在模板空间中产生一个新状态，monad 结构告诉你这些操作怎么组合。

### 1.4 $\mu$ 的幂等性

$\mu_W: T^2(W) \to T(W)$ 是幂等的——模板的模板**退化为**模板。你不会有无穷递归的"元模板"——关于模板的元信息（quality_score, usage_count, confidence）本身就是模板的属性。

### 1.5 Lilies 对应

```python
# η_W: 把工作流注册为模板
POST /api/v1/apps/{id}/publish-template

# T(W): 所有可到达的模板版本
GET  /api/v1/templates/{name}  →  返回当前最新版本
# 历史版本列表维护在 template_store 中

# μ_W: 模板的模板 → 模板
# 当 template_store.merge() 将候选合并到现有模板时，
# 合并后的结果仍然是"一个模板"，不是"模板的模板"
```

---

## 2. Kleisli 范畴：带"副作用"的函数

### 2.1 直觉

普通范畴的态射是 $f: A \to B$（输入 $A$，直接输出 $B$）。

Kleisli 范畴的态射是 $f: A \to T(B)$（输入 $A$，输出"$B$ 的一个模板"）。

**在编程中**：Kleisli 态射 = "带副作用的函数"。副作用是"同时操作了模板市场"。

**在模板市场中**：每个操作都产生或修改模板。

### 2.2 四个操作的 Kleisli 解释

| 操作 | Kleisli 态射 | 读作 |
|------|-------------|------|
| `expand` | $A \to T(A)$ | "输入 $A$，输出 $A$ 的一个模板版本" |
| `publish` | $A \to T(B)$ | "输入 $A$，输出 $B$ 的一个新模板" |
| `merge` | $T(A) \times A \to T(A)$ | "将 $A$ 合并到现有模板中" |
| `evolve` | $T(A) \times A \to T(A) + 1$ | "可能将 $A$ 演化到模板，也可能拒绝 ($+1$ = Maybe)" |

### 2.3 为什么 Kl$(T)$ 包含所有合法操作

> 定理 4.1（模板市场的 Kleisli 范畴）：模板市场的所有合法操作恰好是 $\mathrm{Kl}(T)$ 中的态射。

**翻译**：你不会需要 expand/publish/merge/evolve 之外的模板操作。这四个操作的任意合法组合，恰好构成 Kleisli 范畴中的态射复合。

### 2.4 操作的组合序列

```
expand → publish → merge

这个序列对应于 Kl(T) 中的态射复合:
  A ──expand──→ T(A) ──publish──→ T(B) ──merge──→ T(B)
```

---

## 3. 收敛定理：模板市场会自然收敛

### 3.1 直觉

模板市场不会无限膨胀。随着越来越多的模板被创建和合并，系统会自然收敛到一个稳定状态。

### 3.2 收敛机制

**度量空间**：相似度度量（Jaccard + depth + edges）定义了模板之间的"距离"。

**单调性**：每次 merge 增加 `quality_score`。且 `quality_score ∈ [0, 1]`（有界）。

**收敛证明**（修正版）：
1. 序列 $\{\mathtt{quality\_score}(T_i)\}$ 单调不减 + 有上界 → 收敛到 $q^* \leq 1$
2. 若 $\Delta q_i$ 不趋于 0，则 $q_i \to \infty$，与上界矛盾
3. 因此在有限步内，$\Delta q_i$ 小于任意给定阈值 → 实用收敛

**工程机制**：Lilies 的 `merge_engine.similarity` 阈值（Jaccard ≥ 0.6 时合并而非新建）进一步保证了模板空间不爆炸。

### 3.3 "余极限"的含义

在范畴论中，余极限（colimit）是序列的"汇聚点"。在这里，收敛的模板就是当前知识状态下的余极限——在给定所有已完成合并和演化操作的条件下，不存在更优的模板。

### 3.4 你在论文中看到什么

> 定理 4.2（模板市场的收敛性）：Kleisli 范畴 $\mathrm{Kl}(T)$ 中态射复合的余极限存在。

---

## 4. 用论文串联

### §4.1: 模板 Monad

> 论文：定义 $T$ → $\eta_W$ → $\mu_W$

**翻译**：模板闭包是一个 monad。注册（$\eta$）和合并（$\mu$）满足 monad 律。

### §4.2: Kleisli 范畴

> 论文：定理 4.1（所有合法操作 = $\mathrm{Kl}(T)$ 中的态射）

**翻译**：四个操作构成了模板操作的完备集合。

### §4.3: 收敛

> 论文：定理 4.2（收敛定理）→ 证明

**翻译**：quality_score 单调有界 → 有限步收敛。模板市场不会爆炸。

---

## 5. 自检清单

在进入 §5 之前，确认你能回答：

- [ ] $T(W)$ 的定义是什么？"可达"的含义是什么？
- [ ] $\eta_W$ 在 Lilies 中对应什么 API？
- [ ] $\mu_W$ 为什么是幂等的？"模板的模板"在工程上是什么？
- [ ] Kleisli 范畴的态射和普通范畴的态射有什么区别？
- [ ] 四个操作（expand/publish/merge/evolve）各对应什么 Kleisli 态射？
- [ ] 为什么 evolve 的类型中有 $+1$？
- [ ] 收敛证明的核心论据是什么？（单调有界 → 收敛）
- [ ] `merge_engine.similarity` 阈值的工程作用是什么？

---

## 6. 推荐阅读

| 材料 | 内容 | 预计时间 |
|------|------|---------|
| Moggi (1991) §2-3 | Monad 与 Kleisli 范畴（编程视角） | 2h |
| Mac Lane §VI.5 | Kleisli 范畴的构造 | 1h |
| nLab: Kleisli Category | Kleisli 范畴的定义与性质 | 0.5h |
| 论文 §4 原文 | 结合本笔记精读 | 1h |

---

*这份笔记专门为读懂 `the_pair_categorical.tex` §4 编写。下一阶段（Guide 5）将覆盖 §5——Soundness 作为 Lax Functor：验证函子 $\mathcal{S}$、确定性可判定、非确定性近似、Lax vs Oplax vs Strict。*
