# 架构观察(合并后 lean-core 现状)

> **状态:已更新(2026-08 会话收尾)。**
> 本观察来自对合并前 cluster-theory-l1 的 8 维架构审查,以及合并后 lean-core 的现状核实。
> 每条标注:**已解决 / 部分 / 待核 / 仍适用**。目的是让待办项可追踪,不重复已解决项。

## 1. 安全(P0 — 唯一阻塞级)

| 观察 | 状态 | 说明 |
|------|------|------|
| 默认 token `change-me` 全链路接受(config 默认 / compose 默认 / 前端代理回退),部署时无强制校验 | **已解决** | 三层全修:config.py field_validator 拒绝 change-me/空;compose.yaml 默认改空;前端 route.ts 回退改空(401 拒绝)。启动时默认 token 必失败 |
| Cloudflare 隧道暴露 → 前端代理成为无鉴权全权代理 | **已缓解** | compose.yaml 端口绑定 127.0.0.1(审计确认);Cloudflare 隧道配置在当前 compose 不存在。暴露到公网前仍需显式非默认凭据 + TLS |

**结论**:P0 默认 token 已关闭。

## 2. 执行引擎

| 观察 | 状态 | 说明 |
|------|------|------|
| 三套并行执行引擎(runtime.py Agent 循环 / workflow_runtime.py DAG / builder.py Builder 循环) | **部分** | lean-core 重构删除了 77 个模块,可能已简化;审计显示 workflow_runtime 是中央拓扑串行执行。需核实 runtime.py 是否仍与 workflow_runtime 重复 |
| workflow_runtime 内部串行(无并行节点执行) | **仍适用** | 审计确认:Kahn 拓扑序串行;只有 iteration 积木内并行 |

**建议**:若三引擎仍重复,收敛为一个核心(workflow_runtime 的 DAG 引擎),其余作适配层。

## 3. 数据层

| 观察 | 状态 | 说明 |
|------|------|------|
| 多套持久化机制共享一个 SQLite,无 schema 版本/迁移 | **部分** | schema 版本化已加(D2:PRAGMA user_version=1,更新版本库拒绝打开);但**多类共享一库 + 私有方法越权**仍未收敛(单一存储门面是 P2 大重构) |
| 事件双写(SQLite + JSONL,JSONL 无读者) | **已解决(修正)** | 核实:JSONL 是 DB 归档后的**冷读回退/权威全量副本**(有读者);`archive_events_before` 已在启动时接线(event_archive_keep_days=7),DB 无界增长已斩断 |

**结论**:事件双写是"热 DB + 冷归档"的有意设计,非浪费;schema 版本化已补。剩"多类共享一库"待 P2 大重构。

## 4. 测试

| 观察 | 状态 | 说明 |
|------|------|------|
| 测试漂移(29-35 个失败存在约 6 周) | **已解决** | 合并后已修到全绿(当前 509);修复是人工的,但 CI 会防回归 |
| README 数字漂移(46 vs 61 积木、401 vs 496 测试) | **已解决** | `validate_doc_claims.py` 已写、适配 lean-core、**已进 CI**(C2)——漂移无法无声发生 |

**结论**:文档-实况漂移已闭环(验证器进 CI)。

## 5. 过程

| 观察 | 状态 | 说明 |
|------|------|------|
| 演化控制过程膨胀(190+ 一次性脚本、阶段报告、合同) | **已解决** | lean-core 重构已删除;现在文档结构是精简的(见 docs/README) |
| 治理验证器未强制(建议性) | **部分** | 精简后仍需保留"防漂移验证器"作为硬约束 |

## 6. 仓库卫生

| 观察 | 状态 | 说明 |
|------|------|------|
| references/ 35MB 第三方快照被追踪(占 47% 文件) | **待核** | 核实:仍追踪 1902 文件。lean-core 可能有意保留(设计参考);是否子模块/移除需决策 |
| 根目录散落 eval/demo 脚本 | **已解决** | 核实:只剩 demo_clyins.py 一个;eval_*.py 已随 lean-core 重构清理 |
| 多个项目混仓(platform + git-commit-agent + mobile_app) | **待核** | 与 lean-core 的"lean"理念是否一致,需决策 |

## 7. 集群子系统

| 观察 | 状态 | 说明 |
|------|------|------|
| 集群是平行第二平台(独立 DB、独立遥测、未版本化) | **已解决** | 已整体移除;残留孤件已清理(C1:cluster_scatter_gather.json + cluster_task_market.py 均 0 引用已删) |
| 集群代码中硬编码的"已撤回定理"假检查 | **已解决** | 已随集群移除 |

**结论**:集群孤件已清。

## 8. 前端

| 观察 | 状态 | 说明 |
|------|------|------|
| 页面单体(2000+ 行 page.tsx) | **部分** | lean-core 前端已精简(use/runtime 子页);但 applications/[id]/page.tsx 可能仍较大 |
| 多智能体运行被折叠为单一 "collaborate" 阶段 | **仍适用** | 前端 runtime/use 页面把 subagent/dispatch/mailbox 都映射到同一阶段——团队运行无 per-agent 视图 |

**建议**:若实现团队协作(见 design_bounded_emergence),需拆出 per-agent 视图。

## 总结

**已解决**:P0 安全(默认 token)、集群移除 + 孤件清理、测试全绿(509)、文档-实况闭环(验证器进 CI)、schema 版本化、事件归档核实、可观测性深化(运行基线对比 + 并行事件中继)、根目录脚本清理。

**剩余待办(P2 大重构,风险高需单独规划)**:
1. **三执行引擎收敛**(runtime.py / workflow_runtime.py / builder.py)——最大结构债务;
2. **数据层单一权威**(多类共享一库 + 私有方法越权);
3. 前端 per-agent 视图(团队运行仍折叠为单一 collaborate 阶段);
4. references/ 35MB 快照、多项目混仓的仓库决策。

这些是 P2 结构性重构的输入,不适合"顺手做"。

**溯源**:本观察源自 2026-08 的 8 维架构审查(在多智能体工作流中独立复核)、P1 工程验证、合并后 lean-core 的 3 路代码审计,以及会话收尾的修复与核实。
