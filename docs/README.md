# Lilies docs index

本目录采用文档驱动开发结构。入口顺序如下：

1. `PRODUCT_NORTH_STAR.md`：最高产品意图、目标客户、通用任务原型和场景选择硬门。
2. `PROJECT_EVOLUTION_STRATEGY.md`：文档分层、阶段归档和智力资产筛选规范。
3. `stage-reports/`：当前大版本的小版本阶段报告 active 区，采用 compact factsheet。
4. `stage-report-archives/`：已完成大版本的 stage-report 集合归档。
5. `intellectual-assets/`：少而精的可复用结论，供后续 design 和 plan 引用。
6. `experiment-status/`：当前版本实验闭环、已应用标记和原始实验 backlog 关闭状态。默认读取顺序是索引 -> ledger -> `*_summary.md` -> raw JSON。
7. `workingon/`：当前 stage 的 active 中间结果工作区，归档后只保留 README。
8. `operator-runbooks/`：稳定的操作员流程与运行手册，必须由 stage report 归档引用。
9. `workingon-archives/`：已归档的小版本 workingon 中间材料。
10. `current-design/`：当前 stage 的 active design 工作区，归档后只保留 README。
11. `historical-designs/`：已经具备明确版本 state 的历史 design，只保存最终设计契约。
12. `phase-reports/`：大版本完成后的总复盘和路线转向。
13. `source-materials/`：早期长报告、会议材料、论文草稿和历史证据链。
14. `evolution-control/`：产品意图登记、阶段合同和长任务闭环规则；它约束 stage report，但不产生下一阶段任务。

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

- 产品最高意图：`PRODUCT_NORTH_STAR.md`
- 意图偏移审计：`lilies_product_intent_drift_audit_2026-07-21.md`
- 真实项目实验预注册：`platform_generated_workflow_real_project_experiment_design_2026-07-21.md`
- 候选项目探针证据：`evidence/platform_workflow_candidate_probe_2026-07-21.json`
- 能力边界历史基线：`lilies_agent_scenario_capability_boundary_v0_4_x_latest.docx`，作为底座能力分析使用，不再单独定义产品场景
- Program Charter：`evolution-control/PROGRAM_CHARTER.md`
- 产品与底座意图登记：`evolution-control/report_intents.json`

## 当前阶段报告

当前 active stage report 区域用于当前大版本的小版本报告。`v0.2.x` 和 `v0.3.x` 已完成大版本归档，当前实现历史推进到 `v0.4.11`。

- 模板：`stage-reports/STAGE_REPORT_TEMPLATE.md`
- 最新 active stage report：`stage-reports/v0.4.11_human_journey_usability_repair.md`
- 当前产品方向：原 v0.4.x 能力报告落地作为底座建设保留，但产品完成状态已因真实数据、ML/DL、RAG、预测优化、文档工件和企业/个人真实任务交付缺失而重新打开。电梯和光纤只保留为任务原型示例，不是固定项目。后续阶段必须从 `PRODUCT_NORTH_STAR.md`、真实项目实验预注册和非终态产品意图出发。

## 已归档 Stage Report Sets

- `stage-report-archives/v0.2.x/`：`v0.2.1` 到 `v0.2.144`，共 144 个 stage reports，已由 `phase-reports/v0.2.0_experiment_productization_closeout.md` 收口。
- `stage-report-archives/v0.3.x/`：`v0.3.0` 到 `v0.3.56`，共 57 个 stage reports，已由 `phase-reports/v0.3.0_product_usability_buffer_closeout.md` 收口；归档不等于产品 release-ready，已知回归债保留在 phase report。

## 使用原则

新任务必须先满足 `PRODUCT_NORTH_STAR.md` 的客户与场景硬门，再由最新用户指令或 active `stage-reports/` 的 next-stage task set 排序；如果新大版本尚未创建 active stage report，则由最新 archived handoff stage report 与对应 phase report 确定。当前执行中间材料进入 `workingon/`；需要展开的具体实现计划进入 `current-design/`；只有已经具备明确版本 state 的 design 才归入 `historical-designs/`；小版本结束后归档到 active `stage-reports/`；大版本完成后整批迁入 `stage-report-archives/`；只有经过筛选的复杂结论才进入 `intellectual-assets/`。

实验状态必须维护在 `experiment-status/`。active `workingon/experiment-*` 只保存进行中的实验过程；完成后的 DOCX 报告和 raw evidence 进入 `experiment-status/`，不能替代实验闭环台账。已经用于工程改进的实验必须标记 `已应用` 或 `验证应用`，并补充证据链。常规读取优先索引、单实验 ledger 和 `evidence/*_summary.md`，只有争议或缺字段时再读 raw JSON。

归档后，`current-design/` 和 `workingon/` 必须只保留 README。旧 design 与中间材料分别进入 `historical-designs/` 和 `workingon-archives/`。

## 强制边界

- 产品北极星高于能力报告、阶段报告、技术实验和执行机制。技术场景不能通过阶段闭环自行升级为目标客户场景。
- 下一阶段任务只来自最新 active `stage-reports/` 的 `Next-stage Task Set`；若 active 区为空，则只来自最新 `stage-report-archives/` handoff stage report 与对应 phase report。
- `workingon/` 只保存中间结果、实现证据、实验过程或临时分析；不得作为任务拆解或版本推进的权威来源。
- 新 stage report 必须使用 `stage-reports/STAGE_REPORT_TEMPLATE.md` v2 的固定 Stage Contract、Intent Coverage、Deviation 和 Closure Audit section；没有内容也要写 `none`。历史报告继续按 legacy 合同读取。
- 小版本推进必须有明确 scope justification；连续出现“一个版本只有一个 historical design”应视为阶段切分问题，而不是正常节奏。
- mandatory task 不能由执行者自行延期或降标；`blocked` 不支持版本晋级。归档前运行 `scripts/validate_evolution_control.py`。
