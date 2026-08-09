# 补充证明：读懂 §7.4 的最小概念集

> 目标：能逐行读懂论文 §7.4（补充证明），理解四个定理的形式化——Det 闭包、Retry Soundness、L1 完全性、公理 3 独立性——以及它们各自填补了什么理论缺口。

---

## 0. 大局观：§7.4 在干什么？

初版论文标注了五个"已知局限"——理论覆盖不完整的地方。§7.4 是对其中四个的形式化补证。第五个（Checkpoint 持久化语义）超出了范畴论框架，保留为开放问题。

```
初版的五个局限:
  1. catDet 闭包性质      → §7.4.1 已证明
  2. Error Branch 范畴语义 → §7.4.2 已证明
  3. L1 完全性            → §7.4.3 已证明
  4. 公理 3 独立性        → §7.4.4 已证明
  5. Checkpoint 持久化语义 → 开放问题

这四个证明是论文的重要补充——它们在范畴论框架内
"内在地"解决了初版遗留的理论缺口。
```

---

## 2. 证明一：Det 闭包（§7.4.1）

### 2.1 问题

$\mathbf{Det}$（确定性态射的子范畴）在多大程度上是"封闭的"？
如果我把两个确定性工作流组合起来（顺序、并行、循环），结果还是确定性的吗？

### 2.2 定理

> 定理 7.4.1（$\mathbf{Det}$ 的闭包性）：$\mathbf{Det}$ 在顺序复合 $\circ$、张量积 $\otimes$ 和 trace $\mathrm{Tr}$ 下闭合。

### 2.3 三个引理

**(1) 复合闭合**：若 $f, g \in \mathbf{Det}$，则 $g \circ f \in \mathbf{Det}$

**证明**：$\mathbf{Det}$ 的定义基于"不含 llm/claude_agent 节点"的白名单。合并两个不含 llm 的图不会引入 llm 节点。

**(2) 张量闭合**：若 $f, g \in \mathbf{Det}$，则 $f \otimes g \in \mathbf{Det}$

**证明**：$f \otimes g$ 是 $f$ 和 $g$ 的节点集合的不交并，每个节点保留原始类型。两者都不含 llm → 并集也不含。

**(3) Trace 闭合**：若 $f: A \otimes X \to B \otimes X \in \mathbf{Det}$，则 $\mathrm{Tr}(f): A \to B \in \mathbf{Det}$

**证明**：Trace 对应 Lilies 的 `loop` 积木。`loop` 积木本身是确定性的（只控制控制流，不产生语义内容）。Trace 不修改 $f$ 内部节点的类型。

### 2.4 工程含义

> **并发安全的范畴条件**：仅由 $\mathbf{Det}$ 中态射组成的工作流可以在无额外同步机制的情况下安全地并行执行。

这为 Lilies 中"确定性积木可自由并行，非确定性积木需隔离"的工程实践提供了精确的理论判据。

### 2.5 Lilies 对应

```python
# catDet 的判定：工作流不含 llm/claude_agent 节点
def is_in_catDet(workflow):
    NON_DETERMINISTIC_BLOCKS = {"llm", "claude_agent", "model_turn"}
    return not any(n.type in NON_DETERMINISTIC_BLOCKS for n in workflow.nodes)

# 闭包性质：两个 catDet 工作流的串联仍是 catDet
assert is_in_catDet(W1) and is_in_catDet(W2)
assert is_in_catDet(compose(W1, W2))  # ← 定理保证
```

---

## 3. 证明二：Retry Soundness（§7.4.2）

### 3.1 问题

Lilies 的 Error Branch + Retry 机制是否改变了工作流的"正确语义"？
如果一个 LLM 调用失败了然后重试，最后输出的结果和无错误路径的结果是否语义等价？

### 3.2 形式化定义

**Error 对象**：$\mathsf{Error}$ 是 $\mathbf{WFlow}$ 中的指定对象，代表错误状态。

**带 Error Branch 的态射**：
$$f^{\mathsf{eb}}: A \to B \oplus \mathsf{Error}$$

读作：执行 $f$，若成功输出 $\mathsf{inl}(b)$（正常输出），若失败输出 $\mathsf{inr}(\mathsf{err})$。

**Retry 算子**：
$$R(f^{\mathsf{eb}}) = \mathrm{colim}_{n \to \infty}\; f^{\mathsf{eb}}_n$$

其中 $f^{\mathsf{eb}}_1 = f^{\mathsf{eb}}$，$f^{\mathsf{eb}}_{n+1} = f^{\mathsf{eb}} \circ \pi_A \circ f^{\mathsf{eb}}_n$。$\pi_A$ 是将成功输出投影回 $A$ 的投影。

### 3.3 定理

> 定理 7.4.2（Retry 的 Soundness）：若 $f$ 的失败概率 $p_{\mathsf{fail}} < 1$，则 $R(f^{\mathsf{eb}})$ 在有限步后以概率 1 终止，且输出的语义等价于 $f$ 在无错误路径上的输出。

### 3.4 证明

1. 每次重试的独立成功概率 $p_{\mathsf{succ}} = 1 - p_{\mathsf{fail}} > 0$
2. $n$ 次尝试内成功的概率 $= 1 - p_{\mathsf{fail}}^n \to 1$（当 $n \to \infty$）→ colimit 存在
3. 当第 $k$ 次尝试成功时，$\pi_A \circ f^{\mathsf{eb}}_k = f$（在成功路径上）
4. Colimit 选择第一个成功应用的 $f$ → $R(f^{\mathsf{eb}})$ 的成功输出 = $f$ 的第一次成功输出
5. 与无 Error Branch 的 $f$ 语义完全一致

### 3.5 Lilies 对应

```python
# Lilies 中的 Error Branch + Retry：
# f^eb 对应：error_branch=true 的积木配置
# R(f^eb) 对应：retry=true + max_attempts + delay_seconds

# 工程实现中的指数退避和最大重试次数是 colimit 的有限截断。
```

### 3.6 为什么最大重试次数 = colimit 的有限截断

`colim_{n→∞}` 在工程中不可能真正实现（你不可能重试无限次）。Lilies 的 `max_attempts` 将这个极限截断为有限步。定理保证：**如果截断前的成功概率已经足够高（如 ≥3 次重试覆盖 99.9% 的故障），截断不会显著改变语义。**

---

## 4. 证明三：L1 完全性（§7.4.3）

### 4.1 问题

§6 证明了 L1（4 积木 + 2 组件）是并发安全的**必要**条件。但它是否也是**充分**的？
有没有一些并发安全协议需要 L1 之外的额外原语？

### 4.2 定理

> 定理 7.4.3（L1 的完全性）：设 $\Pi$ 是任意并发安全协议。则 $\Pi$ 可以仅用 $L_1 = \{\mathrm{publish}, \mathrm{subscribe}, \mathrm{acquire}, \mathrm{release}\}$ 和 $\{\mathsf{MessageBus}, \mathsf{ConflictDetector}\}$ 实现。

### 4.3 证明策略（构造性）

对 $\Pi$ 的每个操作类型进行分类并构造归约：

**(a) 纯通信操作**（消息传递、事件通知、状态同步）：
→ 用 publish/subscribe 实现。Fan-Out 和 Fan-In 均可由 publish/subscribe 的复合构造。

**(b) 纯互斥操作**（独占写入、事务性更新）：
→ 用 acquire/release 实现。读共享是 acquire 的 "read" 模式。

**(c) 通信 + 互斥的复合操作**（事务性消息）：
→ 分解为 acquire ∘ publish ⊗ (subscribe ∘ process ∘ release)。

**完备性论证**：任意并发安全协议的操作只有这三种类型。串行化条件已将操作空间完全分类。因此 L1 是完备的。

### 4.4 工程含义

> 不需要第 5 个积木，不需要第 3 个组件。4+2 是并发安全的**充分且必要**的集合。

---

## 5. 证明四：公理 3 独立性（§7.4.4）

### 5.1 问题

公理 3（Pair 幂等）能从公理 1-2 推导吗？如果不能，为什么论文选择它作为公理？

### 5.2 定理

> 定理 7.4.4（公理 3 的独立性）：存在一个满足公理 1-2 但不满足公理 3 的范畴。即公理 3 不能从公理 1-2 导出。

### 5.3 反模型构造：$\mathbf{Cap}_{\mathsf{rec}}$

构造 $\mathbf{Cap}_{\mathsf{rec}}$ 如下：对象和态射与 $\mathbf{Cap}$ 相同，但 functor $P_{\mathsf{rec}}$ 的定义不同：

$$P_{\mathsf{rec}}(f) = H_f \otimes (L_f \circ P_{\mathsf{rec}}(L_f))$$

**读作**：LLM 的非确定性输出被递归地再次输入到另一个 Pair 分解中。

这对应"多层嵌套 Agent"架构——每个 LLM 调用的输出重新触发一个新的 Harness+LLM 循环。

### 5.4 三条公理在 $\mathbf{Cap}_{\mathsf{rec}}$ 中的状态

| 公理 | 状态 | 原因 |
|------|------|------|
| 公理 1（能力二分） | ✅ 成立 | 递归不影响二分性——态射仍然是确定或非确定的 |
| 公理 2（不可约性） | ✅ 成立 | 递归只让 NonDet 变得更深，不是更小 |
| 公理 3（Pair 幂等） | ❌ 不成立 | $P^2$ 包含两层递归嵌套，$P$ 包含一层 → $P^2 \not\cong P$ |

### 5.5 这不是纯理论的构造

$\mathbf{Cap}_{\mathsf{rec}}$ 对应真实的 Agent 架构：

- **AutoGen 的嵌套 GroupChat**：每个 Agent 的输出可以触发一个新的 GroupChat
- **LangGraph 的递归 Supervisor**：Supervisor 可以 spawn 新的 Supervisor
- 这些架构中确实存在 $P^2 \not\cong P$：每层嵌套引入新的协调开销，没有自然饱和点

### 5.6 Lilies 的选择

> Lilies 选择公理 3 意味着**显式拒绝递归 Agent 嵌套**——即拒绝"Agent 的输出本身是一个 Agent"的设计模式。

这是一个**工程决策**，公理 3 使其在数学上精确化：
- 接受公理 3 → 两层迭代后饱和 → 三层必然 → 架构简单可分析
- 拒绝公理 3 → 无限嵌套 → 没有自然饱和点 → 架构复杂度不可控

---

## 6. 第五个局限：Checkpoint 持久化语义

### 6.1 为什么仍旧是开放问题

论文诚实声明：Checkpoint 持久化语义超出了当前范畴论框架的建模能力。

Checkpoint 的 crash-recovery 语义涉及：
- `checkpoint_resume` 的幂等性
- WAL 的持久化保证
- Fault-tolerant state machine replication

这些需要概率 monad（建模故障概率）和幂等 monad（建模 checkpoint）的张量积——目前没有已知的标准构造。

### 6.2 可能的未来方向

论文指出需要 TLA$^{+}$-style 的形式化工具（如 Lamport 的 TLA$^{+}$ 或 Lynch 的 I/O automata），超出了纯范畴论表达。

---

## 7. 用论文串联

### §7.4.1: Det 闭包
> 四个引理（复合、张量、Trace 闭合）→ 定理
> **结论**：确定性工作流的任意组合仍是确定性的

### §7.4.2: Retry Soundness
> 定义 Error 对象 → Error Branch 态射 → Retry 算子 → 概率 1 终止 + 语义保持
> **结论**：Retry 不改成功路径语义

### §7.4.3: L1 完全性
> 分类（通信/互斥/复合）→ 归约构造 → 完备性
> **结论**：4 积木 + 2 组件是并发安全的充分且必要集合

### §7.4.4: 公理 3 独立性
> 构造反模型 $P_{\mathsf{rec}}$ → 验证公理 1-2 成立、公理 3 不成立 → 对应真实递归 Agent 架构
> **结论**：幂等是一个独立的工程选择，不是逻辑必然

---

## 8. 自检清单

读完论文后，确认你能回答：

- [ ] $\mathbf{Det}$ 在哪三种操作下闭合？为什么每个闭合是重要的？
- [ ] Trace 闭合为什么特别？（`loop` 积木的确定性）
- [ ] Error Branch 态射的类型 $A \to B \oplus \mathsf{Error}$ 中 $\oplus$ 是什么？
- [ ] Retry 算子的 colimit 定义是什么？有限截断的工程含义？
- [ ] L1 完全性证明的分类策略是什么？（三种操作类型）
- [ ] $\mathbf{Cap}_{\mathsf{rec}}$ 的 $P_{\mathsf{rec}}$ 定义和标准 $P$ 有什么不同？
- [ ] 为什么 $\mathbf{Cap}_{\mathsf{rec}}$ 不是纯理论的——它在现实中对应什么？
- [ ] Lilies "选择公理 3"意味着拒绝了什么设计模式？
- [ ] Checkpoint 持久化为什么超出了范畴论框架？

---

## 9. 推荐阅读

| 材料 | 内容 | 预计时间 |
|------|------|---------|
| Moggi (1991) §4 | Monad 与异常处理（Error monad 的范畴论） | 1.5h |
| Lynch "Distributed Algorithms" §1-2 | I/O Automata 基础（Checkpoint 语义的形式化方向） | 2h |
| Lamport "Specifying Systems" §1-4 | TLA$^{+}$ 入门 | 2h |
| AutoGen 文档（嵌套 GroupChat） | 公理 3 反模型的真实案例 | 0.5h |
| 论文 §7.4 原文 | 结合本笔记精读 | 1.5h |

---

*这份笔记专门为读懂 `the_pair_categorical.tex` §7.4 编写。这是阅读指南系列的最后一条。恭喜你完成了全部 8 条指南！*
