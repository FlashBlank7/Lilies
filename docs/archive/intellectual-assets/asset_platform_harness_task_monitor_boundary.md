# asset_platform_harness_task_monitor_boundary

## 1. 核心结论

Lilies 需要把所有“会消耗资源、可能长时间运行、可能递归或调度后继续运行”的行为放进 task monitor boundary。这个边界不只是工作流运行时的 `Run`，也应该覆盖 Builder 构建、Agent 工具调用、测试套件、Scheduler 触发、未来的 benchmark/eval 和 live acceptance。

稳定区分：

- 工作流内的 `permission_gate`、`budget_gate`、`sandbox_boundary` 等是 soft harness block。它们提升可表达性和可观察性，但不能当作绝对安全边界。
- 平台外的 Platform Harness 才负责硬约束：任务登记、预算封顶、取消、超时、重试上限、并发限制、审计、状态恢复和异常收敛。
- 发布门禁 `tested_hash == content_hash` 是可靠性边界的一部分，它防止未经重新测试的草稿被发布。

截至 `v0.2.13`，Lilies 的 Platform Harness 已形成一个最小 durable monitor baseline：

- task records 持久化到 `Storage`，可在 app/service recreation 后查询。
- owner-level budgets 可以跨 task 汇总 usage，防止通过拆分任务绕开 per-task budget。
- stale active task reconciliation 可以把过旧 `queued` / `running` task terminalize，避免永久占用 active slots。
- Builder benchmark history 可以直接从 durable benchmark task records 查询。

这个 baseline 仍不是 durable execution：它能让任务边界、预算和历史记录可恢复，但不能恢复崩溃中的执行栈、worker lease 或未完成节点。

## 2. 获得成本

这个结论来自后端核心技术报告、早期 Schedule Trigger token 消耗复盘、Harness 设计推演、当前运行时代码边界梳理，以及 `v0.2.10`-`v0.2.13` 的连续 Platform Harness 工程化阶段。它不是普通“加个限流”的建议，而是 Lilies 从原型走向可控开发的核心治理原则。

## 3. 证据链

- `docs/source-materials/2026-07_initial_architecture_research/Lilies_后端核心技术设计报告.docx`
- `docs/source-materials/2026-07_initial_architecture_research/DESIGN_RATIONALE.md`
- `docs/source-materials/2026-07_initial_architecture_research/MEETING_RESPONSE.md`
- `docs/source-materials/2026-07_initial_architecture_research/BUSINESS_LOGIC.md`
- `docs/stage-report-archives/v0.2.x/v0.2.10_platform_harness_durable_storage.md`
- `docs/stage-report-archives/v0.2.x/v0.2.11_platform_harness_owner_budget.md`
- `docs/stage-report-archives/v0.2.x/v0.2.12_platform_harness_stale_task_reconciliation.md`
- `docs/stage-report-archives/v0.2.x/v0.2.13_builder_benchmark_history.md`

主要代码锚点：

- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/storage.py`
- `platform/backend/src/agent_platform/builder.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `platform/backend/src/agent_platform/runtime.py`
- `platform/backend/src/agent_platform/workflow_storage.py`
- `platform/backend/src/agent_platform/scheduler.py`
- `platform/backend/src/agent_platform/api.py`

## 4. 适用边界

适用于：

- 设计 Platform Harness。
- 设计运行、测试、发布、调度、Builder 构建和 eval 的统一任务监控。
- 复盘资源异常消耗。
- 区分“可表达的软约束”和“不可绕过的硬约束”。

不适用于：

- 把所有产品级规则都塞进单个工作流节点。
- 只靠 prompt 或 Builder 自觉停止来保证安全。
- 用工作流内的 `budget_gate` 替代 API 层、任务层和账号层预算。

## 5. 复用方式

任何新增可运行能力都要先回答：

1. 它是否会调用模型、工具、外部 API、文件系统、沙盒、Scheduler 或测试运行器？
2. 它是否可能循环、递归、重试、并发或跨进程恢复？
3. 它是否有任务 ID、状态机、预算、取消、超时和审计事件？
4. 它的失败是否能被收敛成 `failed`、`needs_attention`、`cancelled` 或 `paused`？

如果答案不清楚，就不能只把它当作普通函数接入。它应该先进入 task monitor boundary 的设计。

## 6. 禁止滥用场景

- 不要把“工作流有 `budget_gate`”写成“平台已有预算硬边界”。
- 不要让 Scheduler、Builder benchmark、live acceptance 绕过任务监控直接运行。
- 不要让发布按钮只看 Builder 的自然语言结论；必须看测试 hash 和发布门禁。
- 不要把 run cancel、build cancel、agent session cancel 当成同一个机制，它们需要统一治理，但代码入口不同。
