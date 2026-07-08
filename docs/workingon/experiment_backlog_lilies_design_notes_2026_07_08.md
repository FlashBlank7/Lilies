# experiment_backlog_lilies_design_notes_2026_07_08

## 1. 实验报告规则

每个实验完成后必须生成一个 `.docx` 报告，放入：

```text
docs/workingon/experiment-reports/
```

命名规则：

```text
YYYY-MM-DD_HHMM_<experiment-topic>.docx
```

示例：

```text
2026-07-08_1730_plan_first_builder_vs_incremental.docx
```

报告结构固定为：

1. 背景
2. 实验设计
3. 结果
4. 结论

如果有图片、流程图、截图或曲线，必须嵌入报告正文并添加简短图注。报告要短、清楚、可读，不写成长论文。

## 2. 实验分组

| 编号 | 实验主题 | 核心问题 | 主要指标 | 产物 |
| --- | --- | --- | --- | --- |
| E01 | Plan-first Builder | 先 plan 再搭 BlockFlow 是否提升复杂任务成功率？ | 成功率、测试通过率、节点数量、成本、人工修复次数 | `.docx` 报告 + 样例工作流 |
| E02 | 单节点增量 vs 模块化增量 | 一次一个节点是否限制复杂度？ | 最大可稳定生成节点数、无效边数、repair cycles、耗时 | `.docx` 报告 |
| E03 | 结构化测试可读性 | 测试输出从 JSON 改成测试框架后，人工是否更容易判断？ | 人工判读时间、误判率、满意度、字段覆盖 | `.docx` 报告 + UI 草图 |
| E04 | 节点级修复 vs 全图重建 | 对“还可以”的工作流，微调节点是否比重建更好？ | 修复成功率、成本、上下文长度、回归失败数 | `.docx` 报告 |
| E05 | Template RAG | 检索模板再展开是否优于从零生成？ | 命中率、展开后测试通过率、成本、速度 | `.docx` 报告 |
| E06 | 模块化工作流复用深度 | workflow module 深度多少开始失控？ | depth、可读性、测试通过率、运行耗时 | `.docx` 报告 |
| E07 | 小模型翻译/中转 | 中文需求先翻译再给小模型是否更稳？ | 结构正确率、语义遗漏率、成本、速度 | `.docx` 报告 |
| E08 | 移除默认 Agent 节点 | 禁用 `claude_agent` 后，显式架构质量是否提升？ | required_node_types 覆盖、可审计性、失败率 | `.docx` 报告 |
| E09 | Harness sidecar | sidecar harness 比工作流内软块更能表达治理边界吗？ | 可解释性、可绕过风险、实现复杂度 | `.docx` 报告 + 架构图 |
| E10 | Tool passmode | `dry_run/approval_required/guarded_auto` 是否降低工具风险？ | 拦截率、误拦截率、人工确认成本 | `.docx` 报告 |
| E11 | 自然语言 Draft Patch | 小改动走 patch 是否优于重跑 Builder？ | 成功率、耗时、上下文长度、测试回归 | `.docx` 报告 |
| E12 | 难度路由 | 根据查询难度选择 operator、depth、model 是否更省成本？ | 成本、质量、失败率、选择准确率 | `.docx` 报告 |
| E13 | 活动记忆助手 | 多天活动记忆是否能提升项目续接能力？ | 找回率、隐私风险、用户确认次数 | `.docx` 报告 |

## 3. 优先级

第一批必须做：

1. E01 Plan-first Builder
2. E03 结构化测试可读性
3. E04 节点级修复 vs 全图重建
4. E05 Template RAG

第二批再做：

1. E02 单节点增量 vs 模块化增量
2. E06 模块化工作流复用深度
3. E09 Harness sidecar
4. E10 Tool passmode

第三批作为助手化路线验证：

1. E07 小模型翻译/中转
2. E11 自然语言 Draft Patch
3. E12 难度路由
4. E13 活动记忆助手

## 4. 报告验收标准

每份实验报告必须回答：

- 实验为什么值得做？
- 对照组是什么？
- 输入样例是什么？
- 指标如何计算？
- 结果是否支持原假设？
- 失败或不显著时，下一步应该停止、修改还是换实验？

如果实验没有真实运行，只能写成实验设计文档，不能命名为完成报告。
