# docs/intellectual-assets 索引

> 更新:2026-08-05。本次更新执行了对 The Pair 范畴论语料的**减法**(详见 [asset_the_pair_core.md](asset_the_pair_core.md) §四)。
>
> **2026-08 适配**:lean-core 采用 **"workflow is server"(工作流即服务)** 定义——[asset_the_pair_core.md](asset_the_pair_core.md) §零 已把 R1-R11 放进"server 为壳、composition 为核"框架;集群层规则(R5/R8/R9)标记为**旧谱系**(lean-core 已移除集群子系统)。

## 权威语料(优先阅读)

| 文档 | 内容 | 状态 |
|------|------|------|
| [asset_the_pair_core.md](asset_the_pair_core.md) | **The Pair 设计公理与规则** —— 权威理论。含 5 条核心规则、设计决策、退出命题清单、开放问题、规则↔代码映射 | ✅ 权威 |
| [asset_engineering_heuristics.md](asset_engineering_heuristics.md) | **工程启发式与检查清单** —— 外部集成检查、新积木判据、测试指导、证据要求 | ✅ 权威 |
| [asset_harness_llm_composite.md](asset_harness_llm_composite.md) | Harness+LLM 复合体(独立小文档) | ✅ 保留 |

## 已被取代的文档(完整论证与历史)

以下文档曾构成 The Pair 的范畴论/形式化表述。经架构审查与独立数学复核,其**范畴论形式化层作为数学不成立**(17 项关键发现全部经对抗性验证为真实,0 项被反驳),但其**工程建模层有真实效力**,已由 core + heuristics 承接。

**这些文档保留在原位置以保存完整推导与历史,不再作为引用入口。** 头部已标注"已被取代"。

| 文档 | 有价值的核心被收编到 | 主要问题(简) |
|------|---------------------|--------------|
| asset_the_pair_categorical.md | core:A1/R3、§三 | Pair monad=恒等函子;Φ 未定义;Yoneda 表述有误;2-category 结构未定义 |
| asset_the_pair_formal_system.md | core:A1/A3/A4/R1-R7/D1-D4 | 定理多为论证而非证明;自由选择/有界迭代/唯一分解有误 |
| asset_cluster_minimality_proof.md | core:R5/R8 | 伴随、自然性不成立;工程论证(P2/P3)本身成立 |
| asset_yoneda_environment_pair_necessity.md | core:A1 修订记录;heuristics §1-4 | "数学必然"论题为非因果跳跃;§7 诚实边界应保留 |
| asset_theoretical_review.md | heuristics §6(四个差距) | "正确性来自必然推演"结论为确认偏差 |
| asset_cross_platform_multi_agent_analysis.md | —(少量) | 层级有界性保证、三个范畴选择断言无依据 |
| asset_cluster_pair_architecture.md | —(少量) | 涌现伪公式 |

> **注**:`asset_the_pair_categorical.md`、`asset_the_pair_formal_system.md`、`asset_harness_llm_composite.md`、`asset_the_pair_poem.md` 有未提交的本地修改(截至 2026-08-05),`asset_yoneda_environment_pair_necessity.md` 尚未跟踪。这些文档的进行中修订请自行决定是否继续;core + heuristics 是当前稳定权威视图。

## 其他资产(不属于被减范畴,保留)

- `asset_blockflow_language_system.md` —— BlockFlow 语言系统
- `asset_clyins_workflow_as_product.md`、`asset_lilies_competitive_strategy.md`、`asset_adaptive_*.md`、`asset_platform_harness_task_monitor_boundary.md` 等 —— 各自独立资产
- `asset_the_pair_poem.md` —— The Pair 诗(作为格言保留,非理论)

## 减法说明

- **时间**:2026-08-05。
- **方式**:不删除任何文件;建立新权威语料 + 声明取代关系 + 原文档标注"已被取代"。所有历史可经 git 追溯。
- **理由**:见 [asset_the_pair_core.md](asset_the_pair_core.md) §四(退出命题清单)。核心判断——工程直觉真实,范畴论包装是空的;**效力与范畴论装饰负相关**。
- **执行审查**:8 维架构审查 + 6 维范畴论数学审查 + 17 项对抗性复核(全部确认,0 反驳)。
