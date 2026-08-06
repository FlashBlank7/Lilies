# 当前阶段报告

本目录只保存当前大版本阶段仍然有效的小版本报告，以及统一的中文阶段报告模板。

## 当前状态

| 字段 | 值 |
| --- | --- |
| 当前大版本 | `v0.4.x` |
| 项目总纲 | `docs/evolution-control/PROGRAM_CHARTER.md` |
| 意图登记表 | `docs/evolution-control/report_intents.json` |
| 最新阶段 | `docs/stage-reports/v0.4.11_human_journey_usability_repair.md` |
| 当前任务权威 | 当前阶段报告中已锁定的“阶段合同”；阶段闭环后则以“下一阶段任务集”为准 |
| 上一大版本归档 | `docs/stage-report-archives/v0.3.x/` |
| 上一大版本总结 | `docs/phase-reports/v0.3.0_product_usability_buffer_closeout.md` |

当前有效报告包括 v0.4.0 至 v0.4.11。v0.2 和 v0.3 的历史报告不再是当前任务来源。

## 规则

- 开始或恢复工作时，先读取最新且通过验证的 v2 阶段报告及其稳定任务 ID。
- “下一阶段任务集”是下一项任务的唯一权威来源；项目总纲和意图登记表约束覆盖范围，但不自行选择下一项工作。
- workingon 只保存中间证据，不能拆解或授权下一阶段任务。
- 实施者不能自行延期、重新分类或降低强制任务的验收标准。
- 版本归档前，必须通过新的闭环审计上下文，以及 `scripts/validate_stage_report_template.py` 和 `scripts/validate_evolution_control.py`。
- 完成大版本时，必须同时归档阶段报告、更新索引、生成大版本总结，并明确交接所有未解决意图。
- 新阶段报告默认使用中文；代码标识、命令、文件路径和固定产品名称可以保留原文。
