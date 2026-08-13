# workflow-as-server 路线图

> **状态:待执行(v1,2026-08)。**
> 基于架构观察(design_architecture_observations_v1)与有界涌现设计(design_bounded_emergence_v1)。
> 原则:客户工作流结果 > 技术机制展示;每步带证据等级(E1-E3)。

## P0 · 收敛与安全(立即)

| # | 任务 | 证据要求 | 状态 |
|---|------|---------|------|
| P0-1 | 把 feat/workflow-as-server 合并回 refactor/lean-core;归档 cluster-theory-l1 | git 历史可追溯 | 待执行 |
| P0-2 | **核实并修复安全基线**:默认 token、前端代理回退、部署时强制校验、Cloudflare 暴露要求非默认凭据 | E1(代码)+ 部署冒烟 | **待核/最高优先** |
| P0-3 | validate_doc_claims.py 挂进发布门/CI | 验证器通过 + CI 报告 | 待执行 |

## P1 · 客户交付(重心所在)

| # | 任务 | 证据要求 | 状态 |
|---|------|---------|------|
| P1-1 | **数据管道场景**:CSV/Excel → 清洗 → typed_workbook 输出工件,端到端跑通 | E2(真实集成) | ✅ 已通过(2026-08,可重跑脚本 examples/p1-scenarios/e2e_data_pipeline.py) |
| P1-2 | **RAG 场景**:文档 → knowledge_retrieval → grounded_answer(带引用) | E2(真实集成) | ✅ 已通过(2026-08,可重跑脚本 examples/p1-scenarios/e2e_rag.py) |
| P1-3 | **企业系统交付**:connector_action 带补偿回写 → 人工复核门 → 审计轨迹 | E2(真实集成)+ E3(生产运行) | ✅ E2 已通过(2026-08,可重跑脚本 examples/p1-scenarios/e2e_enterprise_delivery.py);E3 生产运行待做 |
| P1-4 | 用真实 Builder 搭建上述三个场景(验证 Lilies 自动搭建对客户场景有效) | E2 | 部分(真实 Builder 已验证能搭建投稿工作流;三场景由 API 手工搭建,未用 Builder 全自动) |

**验收状态(2026-08)**:三个场景 E2 证据已记录(见 examples/p1-scenarios/)。**P1-4 待补**:用真实 Builder 让 Lilies 自主搭建这三个场景。

## P2 · 架构卫生

| # | 任务 | 证据要求 | 状态 |
|---|------|---------|------|
| P2-1 | 核实三执行引擎是否仍重复;若重复,收敛为 workflow_runtime 一个核心 | E1(测试全绿) | 待核 |
| P2-2 | 数据层:单一存储门面 + schema 版本化;停止无读者的事件双写 | E1 | 待核 |
| P2-3 | 清理集群残留孤件(cluster_scatter_gather.json、examples/cluster_task_market.py) | git 干净 | 待执行 |
| P2-4 | workflow-as-server 可观测性:事件/追踪/运行记录深化(use/runtime 页面) | E1 | 待执行 |

## P3 · 有界涌现实现(按 design_bounded_emergence_v1)

| # | 任务 | 证据要求 | 状态 |
|---|------|---------|------|
| P3-1 | 恢复 complexity_router,接到 allow_team 门控 | E1(路由单元测试) | 待执行 |
| P3-2 | 写 1 个真实团队场景(投稿双人组:写手+审核) | E2 | 待执行 |
| P3-3 | subagent_spawn 并行选项 + per-agent 前端视图 | E1 + 前端验证 | 待执行 |

## 依赖关系

- P0-2 阻塞所有客户工作(安全不修,客户无法上线)。
- P1 优先于 P2/P3(先证明客户价值,再修架构/加机制——避免重蹈"机制探索替代客户交付"的覆辙)。
- P3 依赖 P1(涌现应服务真实场景,不是为涌现而涌现)。

## 元原则(本路线图的约束)

1. **客户工作流结果 > 技术机制展示**(Product North Star)。
2. **复杂度与所需保证成正比**(lean-core 的核心理念)。
3. **理论是守护,不是装饰**:R1-R4(验证)、R7/R10(组合)、R11(完备相对)、workflow-as-server 框架作为决策依据,不再用范畴论/集群等已撤回内容论证。

**关联**:docs/intellectual-assets/asset_the_pair_core.md(理论核心)、docs/current-design/design_architecture_observations_v1.md、docs/current-design/design_bounded_emergence_v1.md。
