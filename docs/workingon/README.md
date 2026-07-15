# workingon

本目录是当前 stage 的 active intermediate 工作区。

当前状态：`v0.4.6` 已完成并归档；实现、测试与浏览器证据保存在 `docs/workingon-archives/v0.4.6/`。下一任务只从 `docs/stage-reports/v0.4.6_versioned_module_evidence_registry.md` 的 Next-stage Task Set 读取，本目录不拆解任务。

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
