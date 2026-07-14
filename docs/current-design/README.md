# current-design

本目录是当前 stage 的 active design 工作区。

当前状态：v0.3.47 已归档，暂无 active design。

Active designs:

- none

归档规则：

- `current-design` 只保存当前 stage 尚未归档的 `design_*.md`。
- design 只是把一个已接受任务展开成具体实现计划，不具备下一阶段指导权。
- 下一阶段指导权只属于 `docs/stage-reports/`。
- 每个小版本归档时，所有完成、延期、阻塞、替代或拒绝的 design 都必须移动到 `docs/historical-designs/`。
- 归档完成后，本目录应只剩本 README。

历史 design 见：

- `docs/historical-designs/`

如果下一次演进开始时本目录仍有 `design_*.md`，必须先确认它们属于新 stage；否则先归档或清理，不能带着旧 design 继续推进。
