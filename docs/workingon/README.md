# workingon

本目录是当前 stage 的 active intermediate 工作区。

当前状态：`v0.4.3` 正在执行已锁定 Stage Contract；`V04-03-T01C` 至 `T01E` 的实现已完成但产品交互证据待补，当前权威任务是最终集成与浏览器闭环 `V04-03-T01F`。支持的 Browser runtime 当前没有可用浏览器，因此阶段被强制阻塞且不得归档或推进版本。本目录只保留本阶段的中间证据，不提供下一任务。

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
