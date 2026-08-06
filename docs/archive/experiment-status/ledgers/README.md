# Experiment Ledgers

本目录保存 v0.2.x 单实验台账。`../v0.2_experiment_status.md` 是入口索引；本目录文件保存每个实验的状态、关键证据、应用标记和下一步。

读取顺序：

1. 先读 `../v0.2_experiment_status.md`。
2. 再读对应 `ledgers/<experiment>.md`。
3. 默认读 `../evidence/*_summary.md`。
4. 只有争议、字段缺失或要复盘事件轨迹时才读 raw JSON。

维护规则：

- 实验未完成，不得写成工程结论。
- 已用于工程改进，必须标记 `已应用`。
- 只验证已有改进，标记 `验证应用`。
- `.docx` 报告、summary、raw evidence、stage report 必须互相可追溯。
