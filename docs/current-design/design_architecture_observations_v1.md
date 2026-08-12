# 架构观察(合并后 lean-core 现状)

> **状态:待审阅(v1,2026-08)。**
> 本观察来自对合并前 cluster-theory-l1 的 8 维架构审查,以及合并后 lean-core 的现状核实。
> 每条标注:**已解决 / 部分 / 待核 / 仍适用**。目的是让待办项可追踪,不重复已解决项。

## 1. 安全(P0 — 唯一阻塞级)

| 观察 | 状态 | 说明 |
|------|------|------|
| 默认 token `change-me` 全链路接受(config 默认 / compose 默认 / 前端代理回退),部署时无强制校验 | **待核** | 合并后的 lean-core 需核实是否仍存在;若在,这是唯一会直接"烧掉客户"的问题 |
| Cloudflare 隧道暴露 → 前端代理成为无鉴权全权代理 | **待核** | 需确认暴露面与非默认凭据要求 |

**建议**:核实并修复。没有这个,任何客户工作都危险。

## 2. 执行引擎

| 观察 | 状态 | 说明 |
|------|------|------|
| 三套并行执行引擎(runtime.py Agent 循环 / workflow_runtime.py DAG / builder.py Builder 循环) | **部分** | lean-core 重构删除了 77 个模块,可能已简化;审计显示 workflow_runtime 是中央拓扑串行执行。需核实 runtime.py 是否仍与 workflow_runtime 重复 |
| workflow_runtime 内部串行(无并行节点执行) | **仍适用** | 审计确认:Kahn 拓扑序串行;只有 iteration 积木内并行 |

**建议**:若三引擎仍重复,收敛为一个核心(workflow_runtime 的 DAG 引擎),其余作适配层。

## 3. 数据层

| 观察 | 状态 | 说明 |
|------|------|------|
| 多套持久化机制共享一个 SQLite,无 schema 版本/迁移 | **待核** | 33 张表共享一库、无 Alembic 的观察来自旧树;lean-core 需核实 storage/workflow_storage 现状 |
| 事件双写(SQLite + JSONL,JSONL 无读者) | **待核** | 需核实 lean-core 是否仍有此问题 |

**建议**:单一存储门面 + schema 版本化;停止无读者的 JSONL 双写。

## 4. 测试

| 观察 | 状态 | 说明 |
|------|------|------|
| 测试漂移(29-35 个失败存在约 6 周) | **已解决** | 合并后已修到 496 全绿;但修复是人工的,需防回归 |
| README 数字漂移(46 vs 61 积木、401 vs 496 测试) | **已解决** | `validate_doc_claims.py` 已写并适配 lean-core;但**未进 CI/发布门** |

**建议**:把 `validate_doc_claims.py` 挂进发布门/CI,让漂移永远无法无声发生。

## 5. 过程

| 观察 | 状态 | 说明 |
|------|------|------|
| 演化控制过程膨胀(190+ 一次性脚本、阶段报告、合同) | **已解决** | lean-core 重构已删除;现在文档结构是精简的(见 docs/README) |
| 治理验证器未强制(建议性) | **部分** | 精简后仍需保留"防漂移验证器"作为硬约束 |

## 6. 仓库卫生

| 观察 | 状态 | 说明 |
|------|------|------|
| references/ 35MB 第三方快照被追踪(占 47% 文件) | **待核** | lean-core 是否仍追踪;若是,考虑子模块/移除 |
| 根目录散落 eval/demo 脚本 | **部分** | lean-core 可能已清理;核实 |
| 多个项目混仓(platform + git-commit-agent + mobile_app) | **待核** | 与 lean-core 的"lean"理念是否一致,需决定 |

## 7. 集群子系统

| 观察 | 状态 | 说明 |
|------|------|------|
| 集群是平行第二平台(独立 DB、独立遥测、未版本化) | **已解决** | 已按"以 lean-core 为准"整体移除;仅剩孤件(templates/cluster_scatter_gather.json 引用未注册积木、examples/cluster_task_market.py) |
| 集群代码中硬编码的"已撤回定理"假检查 | **已解决** | 已随集群移除 |

**建议**:清理残留孤件(cluster_scatter_gather.json 无法校验/运行,应删除或修复;examples/cluster_task_market.py 归档)。

## 8. 前端

| 观察 | 状态 | 说明 |
|------|------|------|
| 页面单体(2000+ 行 page.tsx) | **部分** | lean-core 前端已精简(use/runtime 子页);但 applications/[id]/page.tsx 可能仍较大 |
| 多智能体运行被折叠为单一 "collaborate" 阶段 | **仍适用** | 前端 runtime/use 页面把 subagent/dispatch/mailbox 都映射到同一阶段——团队运行无 per-agent 视图 |

**建议**:若实现团队协作(见 design_bounded_emergence),需拆出 per-agent 视图。

## 总结

已解决:集群移除、测试全绿、过程瘦身。**待核/P0:安全基线。** 仍适用:数据层、执行引擎收敛、可观测性。这些是下一步(见 workingon/plan_workflow_server_roadmap_v1)的输入。

**溯源**:本观察源自 2026-08 的 8 维架构审查(在多智能体工作流中独立复核)、P1 工程验证,以及合并后 lean-core 的 3 路代码审计。
