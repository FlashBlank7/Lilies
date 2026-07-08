# design_harness_sidecar_and_task_monitor

## 1. 问题

用户提出的疑惑很关键：Harness 不应该只是一堆业务工作流积木。真正的 Harness 与主工作流的关系应有耦合和解耦两部分。

当前 Lilies 已经能在工作流里表达 soft harness blocks，但这些节点可以被草稿删除或绕过。因此它们不能代替 Platform Harness。

## 2. 设计目标

- 把 Harness 表达成主工作流旁边的 sidecar，而不只是主线节点。
- 明确哪些治理能力是工作流内可见的 soft block，哪些是平台外硬边界。
- 让工具监管、预算、取消、审计、重试和恢复统一进入 task monitor boundary。
- 支持 `passmode`，让工具调用有可解释执行策略。

## 3. Harness 分层

| 层 | 位置 | 能否被用户工作流删除 | 例子 |
| --- | --- | --- | --- |
| Soft harness block | `WorkflowSpec` 内 | 可以 | `permission_gate`、`budget_gate`、`event_recorder` |
| Harness sidecar | Run/Build 外挂治理对象 | 不应被工作流删除 | 工具审批、预算事件、checkpoint ledger |
| Platform Harness | API/任务/账号级硬边界 | 不可绕过 | quota、cancel、timeout、sandbox、审计 |

## 4. Sidecar 交互模型

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

## 5. `passmode`

建议定义：

| passmode | 行为 | 适用场景 |
| --- | --- | --- |
| `dry_run` | 只生成工具调用计划，不执行。 | 高风险或调试。 |
| `approval_required` | 执行前必须人工确认。 | 文件写入、外部发布、付费 API。 |
| `guarded_auto` | 自动执行，但受预算、沙盒和审计限制。 | 低风险查询和只读工具。 |
| `manual_only` | 只能人工执行。 | 不可自动化或法规风险动作。 |

## 6. Tool governance 是否需要 plan-first

工具监管不一定需要完整业务 plan-first，但需要最小 execution plan：

- 将要调用什么工具。
- 输入从哪里来。
- 可能产生什么副作用。
- 预算和超时是多少。
- 失败时如何降级。

这可以由 `HarnessSidecar` 在工具执行前生成或检查。

## 7. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `workflow_runtime.py` | 工具调用前发出 governance request。 |
| `runtime.py` | Agent 工具调用也进入相同 sidecar。 |
| `scheduler.py` | schedule fire 进入 task monitor boundary。 |
| `workflow_storage.py` | 保存 sidecar events 和 task monitor records。 |
| `permissions.py` | 增加 passmode 和 approval policy。 |
| `api.py` | 提供任务列表、取消、审批、审计查询。 |

## 8. 验收标准

- 同一套 task monitor 能覆盖 Build、Run、Agent session、Test suite、Scheduler。
- 用户能看到某个工具调用为何被允许、暂停或拒绝。
- 工作流删除 `budget_gate` 后，平台预算仍然生效。
- E09/E10 实验完成并生成 `.docx` 报告。

## 9. 引用资产

- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
