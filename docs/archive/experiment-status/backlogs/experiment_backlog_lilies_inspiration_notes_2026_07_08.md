# experiment_backlog_lilies_inspiration_notes_2026_07_08

## 1. Experiment Report Rule

每个完成的实验必须生成一个简单明了的 `.docx` 报告，放在：

`docs/experiment-status/reports/`

命名：

`YYYY-MM-DD_HHMM_<topic>.docx`

结构固定为：

1. 背景
2. 实验设计
3. 结果结论
4. 图片或截图（如有）

报告必须简练、可读、接近论文逻辑，但不写成论文长度。

## 2. Backlog

| ID | 主题 | 问题 | 方法 | 产物 |
| --- | --- | --- | --- | --- |
| E01 | plan-first vs node-by-node | 先 plan 是否提升复杂 BlockFlow 质量？ | 同一需求分别用当前 Builder 和 plan-first 原型生成，比较节点数、测试通过率、人工修改次数。 | docx |
| E02 | readable TestFrame | 可读测试框架是否降低人工审阅成本？ | 对比 raw JSON test report 与 frame report 的理解时间和错误定位率。 | docx |
| E03 | visible architecture gate | required node/tool gates 是否防止黑箱 Agent 冒充架构？ | 构造 opaque agent 和显式 tool graph，对比 validate/test 结果。 | docx |
| E04 | local repair vs full rebuild | 对还可以的 BlockFlow，局部修复是否优于重新生成？ | 同一失败测试分别局部 patch 和全量 rebuild。 | docx |
| E05 | Template RAG reuse depth | 模板复用深度如何影响质量和复杂度？ | 设置 none/shallow/deep 三档，比较结果。 | docx |
| E06 | translation for small models | 中文需求先翻译是否提升小模型理解？ | 同一中文需求，原文和英文中间表示分别构建。 | docx |
| E07 | complexity router | 根据查询难度设置 operator/workflow depth/model 是否有效？ | 对简单/中等/复杂需求路由不同 Builder 配置。 | docx |
| E08 | Harness sidecar passmode | 工作流旁路 Harness 是否更清楚表达治理？ | 对比 workflow-internal gate 与 sidecar monitor event 设计。 | docx |
| E09 | natural language editing | 画布自然语言修改是否能稳定转成 patch？ | 对修改指令生成 draft operations 并验证。 | docx |
| E10 | assistant memory surface | 多天记忆如何不越过权限边界？ | 设计本地活动摘要、授权读取、可撤销记忆。 | docx |

## 3. Current Status

本文件是原始实验 backlog，不再作为唯一状态源。当前权威状态维护在：

`docs/experiment-status/v0.2_experiment_status.md`

截至 2026-07-09：

- 已完成 3 个 Builder benchmark 相关正式 `.docx` 实验报告。
- 已应用到工程改进的实验必须在实验状态台账和实验报告中标记 `已应用` 或 `验证应用`。
- E01-E10 原始 backlog 尚未逐项关闭；`部分实现` 不等于实验完成。
- 后续 stage 归档前必须更新 `docs/experiment-status/`，并明确每个实验是完成、已应用、延期、阻塞、替代还是未开始。
