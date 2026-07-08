# asset_harness_llm_composite

## 1. 核心结论

Lilies 中可复用的智能能力单元不是裸 LLM，也不是孤立积木，而是 `Harness + LLM` 复合体。LLM 负责非确定性判断、生成和工具选择；Harness 负责上下文组织、工具边界、权限、预算、停止、观测、测试和恢复。

由此得到三个稳定架构原则：

1. 积木不应该无限变聪明；积木应该清楚表达能力边界、输入输出、端口、manual、反模式和错误。
2. 优化重点应该投向组合层：Builder Team、Template、WorkflowRuntime、测试门禁、Platform Harness。
3. Lilies 的长期价值不在“积木数量更多”，而在“被验证过的组合方式更多”。

## 2. 获得成本

这个资产来自多版 `The Primitive Is The Pair` 论文草稿、架构推演、会议概念消歧和后端核心报告。它是跨框架抽象，专门用于解释为什么 Lilies 从 Claude Code 和 Dify 的经验中生长出来后，仍需要建立自己的代码与语言系统。

## 3. 证据链

- `docs/source-materials/2026-07_initial_architecture_research/THE_PRIMITIVE_IS_THE_PAIR_v1_LILIES_CENTRIC_CN.md`
- `docs/source-materials/2026-07_initial_architecture_research/THE_PRIMITIVE_IS_THE_PAIR_v1_LILIES_CENTRIC.md`
- `docs/source-materials/2026-07_initial_architecture_research/THE_PRIMITIVE_IS_THE_PAIR_ORIGINAL.md`
- `docs/source-materials/2026-07_initial_architecture_research/PAPER_OUTLINE.md`
- `docs/source-materials/2026-07_initial_architecture_research/Lilies_竞品研究论文与未来方向报告.docx`

## 4. 适用边界

适用于：

- 判断某个新能力应该做成 block、template、runtime 能力还是 Platform Harness。
- 解释 Agent 架构、workflow 平台和代码智能体之间的共同结构。
- 设计 Builder-as-workflow、Workflow-as-tool 和模板市场。

不适用于：

- 声称所有复杂任务都能靠工作流静态模板解决。
- 忽略模型能力差异。
- 把多智能体团队天然视为强于单智能体。

## 5. 复用方式

设计新功能时先拆分：

| 问题 | 归属 |
| --- | --- |
| 是否需要模型判断、生成、规划、工具选择？ | LLM 层 |
| 是否需要权限、预算、停止、恢复、审计、测试？ | Harness 层 |
| 是否是可复用的节点能力？ | Block 层 |
| 是否是经过验证的组合方式？ | Template / BlockFlow 层 |
| 是否是不可绕过的平台硬边界？ | Platform Harness 层 |

## 6. 禁止滥用场景

- 不要把这个资产理解成“所有东西都应该做成 Agent”。
- 不要因为 `Harness + LLM` 是原子能力单元，就把每个 block 都包成 LLM 调用。
- 不要用这个抽象替代具体代码锚点；落地设计仍必须指向 `WorkflowRuntime`、`AgentRuntime`、`Builder`、`Storage` 或 `Scheduler`。
