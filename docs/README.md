# Lilies docs index

本目录采用文档驱动开发结构。入口顺序如下：

1. `PROJECT_EVOLUTION_STRATEGY.md`：文档分层、阶段归档和智力资产筛选规范。
2. `stage-reports/`：当前大版本的小版本阶段报告 active 区，采用 compact factsheet。
3. `stage-report-archives/`：已完成大版本的 stage-report 集合归档。
4. `intellectual-assets/`：少而精的可复用结论，供后续 design 和 plan 引用。
5. `experiment-status/`：当前版本实验闭环、已应用标记和原始实验 backlog 关闭状态。默认读取顺序是索引 -> ledger -> `*_summary.md` -> raw JSON。
6. `workingon/`：当前 stage 的 active 中间结果工作区，归档后只保留 README。
7. `operator-runbooks/`：稳定的操作员流程与运行手册，必须由 stage report 归档引用。
8. `workingon-archives/`：已归档的小版本 workingon 中间材料。
9. `current-design/`：当前 stage 的 active design 工作区，归档后只保留 README。
10. `historical-designs/`：已经具备明确版本 state 的历史 design，只保存最终设计契约。
11. `phase-reports/`：大版本完成后的总复盘和路线转向。
12. `source-materials/`：早期长报告、会议材料、论文草稿和历史证据链。
13. `evolution-control/`：报告落地 Campaign 的不可变意图约束、稳定意图 ID 和长任务闭环规则；它约束 stage report，但不产生下一阶段任务。

## 当前稳定资产

- `intellectual-assets/asset_blockflow_language_system.md`
- `intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `intellectual-assets/asset_harness_llm_composite.md`
- `intellectual-assets/asset_lilies_competitive_strategy.md`
- `intellectual-assets/asset_adaptive_reuse_defaultization_gate.md`
- `intellectual-assets/asset_adaptive_default_live_acceptance_boundary.md`

## 当前大版本报告

- `phase-reports/v0.2.0_experiment_productization_closeout.md`
- `phase-reports/v0.3.0_product_usability_buffer_closeout.md`

## 当前架构实施基线

- 最新批注吸收报告：`lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx`
- Program Charter：`evolution-control/PROGRAM_CHARTER.md`
- 稳定意图登记：`evolution-control/report_intents.json`

## 当前阶段报告

当前 active stage report 区域用于当前大版本的小版本报告。`v0.2.x` 已完成大版本归档，`v0.3.x` 已进入可用性缓冲完成段，当前推进到 `v0.4.x`。

- 模板：`stage-reports/STAGE_REPORT_TEMPLATE.md`
- 最新 handoff：`stage-report-archives/v0.2.x/v0.2.144_v02x_closeout_and_v03_handoff.md`
- 最新 active stage report：`stage-reports/v0.4.2_report_baseline_and_evolution_control.md`
- 当前 `v0.4.x` 方向：先完成报告落地的防漂移控制层，再按 stage report 推进 Quick/Guided/Governed 分级模式、可选验收、人类可读积木配置、能力合同、模块复用和三类界面。任何 `documented/planned/partial/blocked/deferred` 状态都不能替代真实实现闭环。

## 已归档 Stage Report Sets

- `stage-report-archives/v0.2.x/`：`v0.2.1` 到 `v0.2.144`，共 144 个 stage reports，已由 `phase-reports/v0.2.0_experiment_productization_closeout.md` 收口。
- `stage-report-archives/v0.3.x/`：`v0.3.0` 到 `v0.3.56`，共 57 个 stage reports，已由 `phase-reports/v0.3.0_product_usability_buffer_closeout.md` 收口；归档不等于产品 release-ready，已知回归债保留在 phase report。

## 使用原则

新任务先由最新 active `stage-reports/` 的 next-stage task set 确定；如果新大版本尚未创建 active stage report，则由最新 archived handoff stage report 与对应 phase report 确定。当前执行中间材料进入 `workingon/`；需要展开的具体实现计划进入 `current-design/`；只有已经具备明确版本 state 的 design 才归入 `historical-designs/`；小版本结束后归档到 active `stage-reports/`；大版本完成后整批迁入 `stage-report-archives/`；只有经过筛选的复杂结论才进入 `intellectual-assets/`。

实验状态必须维护在 `experiment-status/`。active `workingon/experiment-*` 只保存进行中的实验过程；完成后的 DOCX 报告和 raw evidence 进入 `experiment-status/`，不能替代实验闭环台账。已经用于工程改进的实验必须标记 `已应用` 或 `验证应用`，并补充证据链。常规读取优先索引、单实验 ledger 和 `evidence/*_summary.md`，只有争议或缺字段时再读 raw JSON。

归档后，`current-design/` 和 `workingon/` 必须只保留 README。旧 design 与中间材料分别进入 `historical-designs/` 和 `workingon-archives/`。

## 强制边界

- 下一阶段任务只来自最新 active `stage-reports/` 的 `Next-stage Task Set`；若 active 区为空，则只来自最新 `stage-report-archives/` handoff stage report 与对应 phase report。
- `workingon/` 只保存中间结果、实现证据、实验过程或临时分析；不得作为任务拆解或版本推进的权威来源。
- 新 stage report 必须使用 `stage-reports/STAGE_REPORT_TEMPLATE.md` v2 的固定 Stage Contract、Intent Coverage、Deviation 和 Closure Audit section；没有内容也要写 `none`。历史报告继续按 legacy 合同读取。
- 小版本推进必须有明确 scope justification；连续出现“一个版本只有一个 historical design”应视为阶段切分问题，而不是正常节奏。
- mandatory task 不能由执行者自行延期或降标；`blocked` 不支持版本晋级。归档前运行 `scripts/validate_evolution_control.py`。
