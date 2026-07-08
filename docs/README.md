# Lilies docs index

本目录采用文档驱动开发结构。入口顺序如下：

1. `PROJECT_EVOLUTION_STRATEGY.md`：文档分层、阶段归档和智力资产筛选规范。
2. `stage-reports/`：小版本阶段报告，说明每个 stage 完成了什么、留下什么任务。
3. `intellectual-assets/`：少而精的可复用结论，供后续 design 和 plan 引用。
4. `workingon/`：当前 stage 的任务级 plan、中间实验和临时结果。
5. `current-design/`：当前任务的具体设计和实现报告。
6. `phase-reports/`：大版本完成后的总复盘和路线转向。
7. `source-materials/`：早期长报告、会议材料、论文草稿和历史证据链。

## 当前稳定资产

- `intellectual-assets/asset_blockflow_language_system.md`
- `intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `intellectual-assets/asset_harness_llm_composite.md`
- `intellectual-assets/asset_lilies_competitive_strategy.md`

## 当前阶段报告

- `stage-reports/V1.1_docs_consolidation_and_asset_baseline.md`

## 当前 workingon

- `workingon/plan_apply_lilies_design_notes_2026_07_08.md`：当前 stage 主计划和状态表。
- `workingon/question_log_lilies_backend_design_2026_07_08.md`：机制问题回答。
- `workingon/experiment_backlog_lilies_design_notes_2026_07_08.md`：实验 backlog 和 `.docx` 报告规则。

## 当前具体设计

- 具体状态见 `current-design/README.md`。

## 使用原则

新任务先进入未来的 `workingon/` task plan；需要审阅的实现细节进入未来的 `current-design/`；阶段结束后归档到 `stage-reports/`；只有经过筛选的复杂结论才进入 `intellectual-assets/`。
