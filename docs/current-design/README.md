# current-design

本目录是当前 stage 的 active design 工作区。

当前状态：`v0.4.3` 已冻结合同，六个 mandatory task 正在展开。

Active designs:

- `design_v043_regression_time_boundary.md` -> `V04-03-T01A`
- `design_v043_delivery_mode_policy.md` -> `V04-03-T01B`
- `design_v043_evidence_publish_lifecycle.md` -> `V04-03-T01C`
- `design_v043_acceptance_repair_context.md` -> `V04-03-T01D`
- `design_v043_schema_driven_block_config.md` -> `V04-03-T01E`
- `design_v043_integrated_browser_closure.md` -> `V04-03-T01F`

归档规则：

- `current-design` 只保存当前 stage 尚未归档的 `design_*.md`。
- design 只是把一个已接受任务展开成具体实现计划，不具备下一阶段指导权。
- 下一阶段指导权只属于 `docs/stage-reports/`。
- 每个小版本归档时，所有完成、延期、阻塞、替代或拒绝的 design 都必须移动到 `docs/historical-designs/`。
- 归档完成后，本目录应只剩本 README。

历史 design 见：

- `docs/historical-designs/`

如果下一次演进开始时本目录仍有 `design_*.md`，必须先确认它们属于新 stage；否则先归档或清理，不能带着旧 design 继续推进。
