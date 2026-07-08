# current-design

本目录保存当前开发任务的具体设计和实现报告。`current-design` 不是最终交付物，而是实现契约：除非用户明确要求 design-only，否则创建设计后必须继续落代码、验证，并把证据写入 `docs/workingon/`。

一个 task plan 如果需要展开模块边界、数据流、实现方案、风险和验收标准，就应该在这里新增 `design_<component-or-flow>.md`。设计文档应优先引用 `intellectual-assets/` 中的稳定结论。

当前保留的设计：

- `design_platform_harness_task_monitor_v1.md`
- `design_builder_benchmark_v1.md`
- `design_builder_benchmark_suite_v1.md`
- `design_builder_test_self_consistency_v1.md`
- `design_paid_builder_benchmark_experiment_v1.md`
- `design_natural_language_draft_patch_preview.md`
- `design_platform_harness_observability_ui_v1.md`

注意：design 只有在对应版本 state 或 stage report 明确出现，并标记最终状态后，才可以归档。没有明确版本状态时，应保留在本目录，或者先拒绝归档并补齐版本状态。

v0.2.2 对应的 design 已按版本归档到：

- `docs/historical-designs/v0.2.2_design_archive_manifest.md`

v0.2.3 对应的 design 已按版本归档到：

- `docs/historical-designs/v0.2.3_design_platform_harness_task_monitor_v1.md`
- `docs/historical-designs/v0.2.3_design_builder_benchmark_v1.md`
- `docs/historical-designs/v0.2.3_design_natural_language_draft_patch_preview.md`

v0.2.4 对应的 design 已按版本归档到：

- `docs/historical-designs/v0.2.4_design_platform_harness_observability_ui_v1.md`

v0.2.5 对应的 design 已按版本归档到：

- `docs/historical-designs/v0.2.5_design_builder_benchmark_suite_v1.md`

v0.2.6 对应的 design 已按版本归档到：

- `docs/historical-designs/v0.2.6_design_paid_builder_benchmark_experiment_v1.md`

v0.2.7 对应的 design 已按版本归档到：

- `docs/historical-designs/v0.2.7_design_builder_test_self_consistency_v1.md`
