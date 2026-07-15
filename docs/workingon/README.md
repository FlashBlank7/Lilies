# workingon

本目录是当前 stage 的 active intermediate 工作区。

当前状态：`v0.4.5` 合同已锁定；实现证据只记录在本目录，当前任务从阶段报告的 `V04-05-T01A` 读取。

归档规则：

- `workingon` 只保存当前 stage 的执行中间结果、实现证据、问题记录和临时判断。
- `workingon` 不具备下一阶段指导权。
- 下一阶段指导权只属于 `docs/stage-reports/`。
- 每个小版本归档时，所有中间文件必须移动到 `docs/workingon-archives/v<version>/` 或更合适的版本化证据目录。
- 实验报告和实验原始证据应进入 `docs/experiment-status/`。
- 阶段执行中可保留当前版本的 checkpoint；归档完成后必须连同 checkpoint 一起迁入版本归档，本目录只剩本 README。

历史 workingon 证据见：

- `docs/workingon-archives/`
- `docs/experiment-status/`

如果下一次演进开始时本目录仍有中间文件，必须先判断它们是否属于新 stage；否则先归档或清理，不能带着旧中间文件继续推进。
