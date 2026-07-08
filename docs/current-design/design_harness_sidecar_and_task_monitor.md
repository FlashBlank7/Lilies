# design_harness_sidecar_and_task_monitor

状态：已完成  
对应 plan：`docs/workingon/plan_apply_lilies_design_notes_2026_07_08.md`  
完成日期：2026-07-08  
设计性质：下一阶段实现设计，不直接修改后端代码

## 1. 问题

用户提出的疑惑很关键：Harness 不应该只是一堆业务工作流积木。真正的 Harness 与主工作流的关系应有耦合和解耦两部分。

当前 Lilies 已经能在工作流里表达 soft harness blocks，但这些节点可以被草稿删除或绕过。因此它们不能代替 Platform Harness。

## 2. 设计目标

- 把 Harness 表达成主工作流旁边的 sidecar，而不只是主线节点。
- 明确哪些治理能力是工作流内可见的 soft block，哪些是平台外硬边界。
- 让工具监管、预算、取消、审计、重试和恢复统一进入 task monitor boundary。
- 支持 `passmode`，让工具调用有可解释执行策略。

非目标：

- 不把工作流内 soft harness block 误写成不可绕过的安全边界。
- 不在第一版重写全部运行时。
- 不把 Scheduler、Builder、Test suite 排除在 task monitor 之外。
- 不用 prompt 约束替代平台硬边界。

## 3. Harness 分层

| 层 | 位置 | 能否被用户工作流删除 | 例子 |
| --- | --- | --- | --- |
| Soft harness block | `WorkflowSpec` 内 | 可以 | `permission_gate`、`budget_gate`、`event_recorder` |
| Harness sidecar | Run/Build 外挂治理对象 | 不应被工作流删除 | 工具审批、预算事件、checkpoint ledger |
| Platform Harness | API/任务/账号级硬边界 | 不可绕过 | quota、cancel、timeout、sandbox、审计 |

判断规则：

- 如果一个约束能被 Builder 或用户删除，它就是 soft harness。
- 如果一个约束必须覆盖所有 Build/Run/Test/Scheduler，它属于 Platform Harness。
- Harness sidecar 是执行期的治理对象，负责把主线运行和平台硬边界连接起来。

## 4. 核心对象

### `TaskMonitorRecord`

统一记录资源消耗任务。

| 字段 | 含义 |
| --- | --- |
| `task_id` | 任务 ID。 |
| `task_kind` | `build` / `run` / `agent_session` / `test_suite` / `scheduler_fire` / `benchmark`。 |
| `owner` | 用户、应用、版本或系统任务。 |
| `status` | `queued` / `running` / `paused` / `cancelled` / `failed` / `succeeded` / `needs_attention`。 |
| `budget_limit` | token、美元、时间、工具调用数等预算。 |
| `budget_used` | 当前消耗。 |
| `cancel_requested` | 是否收到取消请求。 |
| `started_at` / `updated_at` / `finished_at` | 生命周期时间。 |
| `audit_ref` | 审计事件引用。 |
| `parent_task_id` | 子任务追踪，如 Builder 触发 test suite。 |

### `SidecarRequest`

运行时向 sidecar 询问是否允许继续。

| 字段 | 含义 |
| --- | --- |
| `request_id` | 请求 ID。 |
| `task_id` | 所属 task。 |
| `request_kind` | `tool_call` / `budget_check` / `sandbox_open` / `checkpoint` / `external_publish`。 |
| `actor` | WorkflowRuntime、AgentRuntime、Scheduler 或 Builder。 |
| `resource` | 工具、URL、文件、模型、沙盒等。 |
| `proposed_action` | 即将执行的动作。 |
| `risk_level` | `low` / `medium` / `high` / `blocked`。 |
| `passmode` | 执行策略。 |
| `evidence` | 工具输入、plan、测试上下文等证据。 |

### `SidecarDecision`

| 字段 | 含义 |
| --- | --- |
| `decision` | `allow` / `deny` / `pause_for_approval` / `modify` / `dry_run_only`。 |
| `reason` | 决策原因。 |
| `modified_action` | 如果修改了请求，记录新动作。 |
| `expires_at` | 临时授权过期时间。 |
| `audit_event_id` | 审计事件。 |

## 5. Sidecar 交互模型

主工作流和 sidecar 通过事件和请求通信：

```text
WorkflowRuntime
  -> emits node.started / tool.requested / budget.requested
HarnessSidecar
  -> approves / rejects / modifies / pauses
WorkflowRuntime
  -> continues / pauses / fails / degrades
TaskMonitor
  -> records status, budget, owner, cancel signal, audit
```

这种关系像 UML 中两条不同的线：

- 主业务线：节点之间的数据流。
- Harness 线：治理事件、授权和资源生命周期。

关键不变量：

- 主运行时不能直接绕过 sidecar 执行高风险工具。
- sidecar 不能修改 `WorkflowSpec`，只能允许、拒绝、暂停或修改执行请求。
- 所有 sidecar decision 必须可审计。
- cancel 信号由 task monitor 持有，运行时必须定期检查。

## 6. `passmode`

建议定义：

| passmode | 行为 | 适用场景 |
| --- | --- | --- |
| `dry_run` | 只生成工具调用计划，不执行。 | 高风险或调试。 |
| `approval_required` | 执行前必须人工确认。 | 文件写入、外部发布、付费 API。 |
| `guarded_auto` | 自动执行，但受预算、沙盒和审计限制。 | 低风险查询和只读工具。 |
| `manual_only` | 只能人工执行。 | 不可自动化或法规风险动作。 |

执行语义：

- `dry_run` 必须返回计划和风险，不允许产生副作用。
- `approval_required` 必须让 task 进入 `paused`，直到人工批准或取消。
- `guarded_auto` 必须先检查预算、沙盒、工具白名单和审计记录。
- `manual_only` 必须拒绝自动执行，并给出人工操作说明。

## 7. Tool governance 是否需要 plan-first

工具监管不一定需要完整业务 plan-first，但需要最小 execution plan：

- 将要调用什么工具。
- 输入从哪里来。
- 可能产生什么副作用。
- 预算和超时是多少。
- 失败时如何降级。

这可以由 `HarnessSidecar` 在工具执行前生成或检查。

## 8. Task monitor 覆盖范围

必须纳入 task monitor boundary：

| 行为 | 原因 |
| --- | --- |
| Builder build | 会调用模型、工具、测试，可能 repair 循环。 |
| Workflow run | 会执行节点、工具、模型、human pause。 |
| Agent session | 会多轮工具调用和子 agent 递归。 |
| Test suite | 可能运行真实工具和 LLM judge。 |
| Scheduler fire | 无人值守触发，必须有 owner、预算和取消。 |
| Benchmark/eval | 可能批量调用模型和工具。 |
| Template reindex | 可能批量读取、评分、生成 embedding。 |

不变量：

- 任何资源消耗任务必须有 `task_id`。
- 任何无人值守任务必须有 owner 和预算。
- 任何长任务必须可取消。
- 任何外部副作用必须有 audit event。

## 9. 状态模型

```text
queued
  -> running
  -> paused
  -> running
  -> succeeded

running
  -> failed
  -> needs_attention
  -> cancelled
```

状态含义：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已登记，尚未开始。 |
| `running` | 正在消耗资源。 |
| `paused` | 等待人工输入、审批或外部事件。 |
| `needs_attention` | 自动流程无法继续，但保留上下文。 |
| `cancelled` | 用户或系统取消。 |
| `failed` | 不可恢复错误。 |
| `succeeded` | 正常完成。 |

## 10. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `workflow_runtime.py` | 工具调用前发出 governance request。 |
| `runtime.py` | Agent 工具调用也进入相同 sidecar。 |
| `scheduler.py` | schedule fire 进入 task monitor boundary。 |
| `workflow_storage.py` | 保存 sidecar events 和 task monitor records。 |
| `permissions.py` | 增加 passmode 和 approval policy。 |
| `api.py` | 提供任务列表、取消、审批、审计查询。 |

### 10.1 第一版最小实现

1. 增加 `TaskMonitorRecord` 数据结构。
2. Build、Run、Test suite 创建 task record。
3. `workflow_runtime.py` 和 `runtime.py` 在工具调用前构造 `SidecarRequest`。
4. sidecar 第一版只实现 budget、cancel、passmode 和 audit。
5. API 提供 task list、task detail、cancel、approval decision。

### 10.2 第二版扩展

- Scheduler fire 全量接入 task monitor。
- Sandbox lifecycle 纳入 sidecar。
- Benchmark/eval 纳入统一任务边界。
- 前端任务监控面板。
- Sidecar decision policy 可配置。

## 11. 迁移阶段

| 阶段 | 目标 | 退出条件 |
| --- | --- | --- |
| M1：记录 | 先把 Build/Run/Test 登记为 task。 | 能看到任务列表和状态。 |
| M2：取消 | 统一 cancel 信号。 | Build/Run/Test 能被取消并收敛状态。 |
| M3：工具请求 | 工具调用前发 sidecar request。 | 高风险工具可暂停审批。 |
| M4：预算 | token、时间、工具次数进入预算。 | 超预算任务自动停止或 needs_attention。 |
| M5：Scheduler | Scheduler 只 enqueue monitored task。 | 无人值守任务可追踪、可取消。 |

## 12. 风险与约束

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| sidecar 过度侵入运行时 | 实现复杂、bug 增多。 | 第一版只拦工具、预算、取消。 |
| 状态模型和现有 Build/Run 状态冲突 | 维护成本增加。 | task monitor 作为外层记录，不替代内部状态。 |
| approval_required 造成流程卡死 | 用户体验差。 | 超时、提醒、取消和降级策略。 |
| Scheduler 绕过 task monitor | 无人值守资源消耗。 | Scheduler 只能 enqueue monitored task。 |
| soft harness 被误当硬边界 | 安全错觉。 | 文档和 UI 明确软硬分层。 |

## 13. 实验切片

对应实验：

- E09：Harness sidecar。
- E10：Tool passmode。
- E12：难度路由。

第一批实验：

| 实验 | 最小样例 | 指标 |
| --- | --- | --- |
| E09 | 一个含外部工具的 workflow run。 | 可解释性、绕过风险、实现复杂度。 |
| E10 | 文件写入、只读查询、外部发布三类工具。 | 拦截率、误拦截率、人工确认成本。 |
| E12 | 不同难度任务映射 passmode 和模型。 | 成本、失败率、选择准确率。 |

## 14. 验收标准

- 同一套 task monitor 能覆盖 Build、Run、Agent session、Test suite、Scheduler。
- 用户能看到某个工具调用为何被允许、暂停或拒绝。
- 工作流删除 `budget_gate` 后，平台预算仍然生效。
- Scheduler 触发的任务必须有 owner、预算、状态和取消入口。
- `approval_required` 能让任务进入 `paused` 并等待明确审批。
- `guarded_auto` 执行必须写 audit event。
- E09/E10 实验完成并生成 `.docx` 报告。

## 15. 完成证据

本设计已补齐：

- Harness 分层判断规则。
- `TaskMonitorRecord`、`SidecarRequest`、`SidecarDecision`。
- sidecar 交互不变量。
- passmode 执行语义。
- task monitor 覆盖范围。
- 状态模型。
- 迁移阶段、风险和实验切片。
- 可执行验收标准。

因此本文件可以作为下一阶段实现 Platform Harness sidecar 和 task monitor boundary 的设计依据。

## 16. 引用资产

- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
