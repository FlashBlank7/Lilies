# 有界涌现(Bounded Emergence)设计

> **状态:待审阅(v1,2026-08)。**
> 背景:团队对"是否该让智能体主动涌现协作"存在分歧(保守方:人主动请;涌现方:主动协作优于个体)。本设计给出裁决与实现方案,并记录对元胞自动机(CA)的借鉴与不移植之处。
> 依据:对合并后 lean-core 的 3 路代码审计(orchestration primitives / complexity-router / team-scenarios-observability)。

## 1. 裁决:不是"要不要涌现",是"什么阈值、如何约束"

> **默认不涌现(保守方对);当复杂度/不确定性超过阈值,由确定性机制显式组装团队(涌现方对);全程预算约束、完全可观测。**

两方各对一半、各错一半:
- **保守方对**:简单任务单单元,零涌现开销(实测:公众号投稿单工作流足够)。
- **保守方错**:人成为复杂任务的瓶颈——人不可能总预见到"需要一个团队"。
- **涌现方对**:复杂任务集体真实优于个体(多智能体链已验证:分治+并行专家+评审环)。
- **涌现方错**:无界涌现不可控(这是移除 cluster_emergent 的原因——不是"涌现错了",是"无界涌现不可控")。

## 2. 现状审计(3 路代码审计结论)

| 事实 | 证据 |
|------|------|
| 工作流内部协作 100% 显式 | 中央 Kahn 拓扑序串行执行(workflow_runtime.py:1343-1448);协作积木是被动局部规则 |
| 工作流内部无自主涌现 | subagent_spawn 硬设 allow_subagents=False(:2903);tool_executor 注入 no_subagent |
| 涌现只存在于图外 3 处 | AgentRuntime Agent 工具(深度≤2);Builder spawn_teammate(有上限);workflow:`<app>` 嵌套(白名单) |
| **无任何"何时升级到团队"的路由** | complexity_router.py 已被 lean-core 删除(可从 git 恢复);休眠字段存在但从不写入;allow_team 无条件开放 |
| 零团队场景 | ScenarioCatalog 只有 3 个单智能体蓝图;多智能体积木线性硬编码 |
| 团队运行观测 = 单个工作流 | 前端折叠为单一 "collaborate" 阶段;无 per-agent 视图 |

## 3. 元胞自动机(CA)的借鉴与不移植

### 可借鉴的三条原则

1. **"局部规则 → 全局涌现,无需中央控制"**——默认交互应极简、局部(每个智能体只与声明邻域交互),全局行为由这些规则涌现。CA 证明这不需要重型中央编排。
2. **"边缘混沌"(edge of chaos)是涌现阈值的设计旋钮**——最有用的行为在有序与混沌的边界。涌现阈值不是拍脑袋,而是一个相变边界。
3. **"涌现可观测,不可预测"**——CA 长期行为常不可判定(Rule 110 图灵完备);涌现式协作同理:不静态验证(R4/R11),要观测(事件轨迹、成本记录)。

### 不可移植的三点

1. **CA 细胞同质、无代价、无目标;Agent 异质、昂贵、有目标**——CA 涌现免费,Agent 涌现花钱,必须预算。
2. **CA 是"看网格演化";我们要"朝目标交付结果"**——涌现必须目标导向。
3. **不能把系统"形式化"成 CA**(同范畴论教训)——CA 是启发,不是模型。

## 4. 设计:四个层级

### 层级 1 · 相位触发器(补齐"边缘混沌"——现在缺失)
- 恢复 `complexity_router.py`(可从 git 恢复:信号关键词 → simple/medium/complex)。
- 输出改为:`allow_team`(是否允许协作)+ `planning_mode`——即"单单元 vs 团队"的相变翻转。
- 接线:`POST /builds` 入口(api.py:4328-4362)或 `builder._run` 顶部(builder.py:407),写入**已存在的休眠字段**(workflow_models.py:476-477 `complexity_router`/`runtime_builder_policy`),门控当前无条件开放的 `allow_team`(builder.py:843-844)。

### 层级 2 · 显式团队组装(全局模式由局部规则涌现)
- 触发 `allow_team=true` 后,团队**显式组装**(确定性,不是智能体自发):
  - 角色分解 + 依赖图 → `task_dispatcher`(已存在)
  - 执行 → `subagent_spawn`(已存在)
  - 协调 → `dependency_gate` + `mailbox_wait_wake`(已存在)

### 层级 3 · 并行局部规则(CA 的"同步并行"——现在最缺)
- CA 的涌现来自所有细胞并行按局部规则更新;我们的工作流是串行的。
- **设计**:团队被组装后,独立专家**并行运行**(参考 iteration 积木的 `asyncio.gather` 模式,workflow_runtime.py:813-835),每个 Agent 只通过 $ref/mailbox 与其**声明邻域**交互。
- 全局协作模式(评审环、流水线)从并行局部交互中涌现,而非一串串行脚本。

### 层级 4 · 边界与可观测(CA 的"全格可观测")
- **预算边界**(已有):budget_gate、round_limit、teammate 上限、`_teammate_guard_reason`(builder.py:2089-2112)——CA 的 "Class IV 边界框"。
- **可观测性(缺)**:
  - 前端拆出 per-agent 独立阶段(当前折叠为单一 "collaborate")。
  - 团队级遥测:谁被组装、为什么、每个 agent 的 turn/cost/结果。
  - 每次升级记录"触发信号 + 决策 + 预算"。

## 5. 与现有代码的衔接

| 组件 | 状态 | 设计中的角色 |
|------|------|-------------|
| subagent_spawn / task_dispatcher / dependency_gate / mailbox_wait_wake | ✅ 已存在 | 团队执行/组装原语(局部规则) |
| budget_gate / round_limit / teammate 上限 | ✅ 已存在 | 预算边界 |
| spawn_teammate / send_message(Builder) | ✅ 已存在(有界涌现) | 协调者的"局部规则" |
| **complexity_router** | ❌ 已删(可恢复) | 相位触发器(层级 1) |
| **allow_team 门控** | ❌ 当前无条件 | 触发器输出 → 团队可用性 |
| **并行团队执行** | ❌ 工作流内无并行 | CA 的同步并行局部更新 |
| **per-agent 可观测性** | ❌ 折叠为单阶段 | CA 的全格可观测 |
| **团队场景蓝图** | ❌ 零个 | 用现有积木组装 1-2 个真实场景 |

## 6. 落地步骤

1. 恢复 complexity_router,接到 allow_team 门控(最小改动,复用休眠字段)。
2. 写 1 个真实团队场景(如"投稿双人组":写手 + 审核,用 subagent_spawn + mailbox 构成评审环),验证层级 2。
3. 给 subagent_spawn 加并行选项(gather 多个独立子 agent),实现层级 3。
4. 拆前端 per-agent 视图 + 团队遥测(层级 4)。
5. 用三个真实客户场景(数据/RAG/企业API,见 roadmap)验证"单单元 → 路由触发 → 团队协作"闭环。

## 7. 诚实边界

- 本设计不引入新"涌现框架";它复用现有积木 + 一个被删但可恢复的路由器。
- 与理论内核的关系:R11(完备相对)→ 协作模式是需求驱动;R4(验证上界)→ 不静态验证涌现,观测它;workflow-as-server → 协作是服务组合。
- 不移植 CA 的"无代价自由涌现"——每次升级都受预算与可观测约束。

## 8. 实现状态(2026-08,feat/workflow-as-server)

| 层级 | 内容 | 状态 |
|------|------|------|
| **1 · 相位触发器** | `complexity_router.py`(确定性 classify_requirement);api.py create_build 写入 team_state.complexity_router;builder.py `_agent_loop` 门控 allow_team(默认保守) | ✅ 已实现 + 测试 |
| **2 · 显式团队组装** | `submission_team` 场景(写手+审核 subagent_spawn,依赖图+独立预算),注册进 ScenarioCatalog | ✅ 已实现 + 端到端测试 |
| **3 · 并行局部规则** | `parallel_agents` 积木:多个独立子智能体 asyncio.gather 并行执行,输出聚合为 name→result | ✅ 已实现 + 测试 |
| **4 · 边界与可观测** | 预算边界(已有);**build 决策可观测**(GET build → team_state.complexity_router.allow_team);**agent 事件可观测**(agent.started/completed 含 usage) | ✅ 后端已实现;前端 per-agent 视图为后续项 |

**仍待办(不在本次范围)**:
- 前端 runtime/use 页拆出 per-agent 独立阶段(当前折叠为单一 "collaborate")。
- 团队级遥测聚合视图(谁/为什么/每 agent 的 turn/cost/result 的集中展示)。
- 三个真实客户场景(数据/RAG/企业API)的端到端闭环验证(见 roadmap)。
