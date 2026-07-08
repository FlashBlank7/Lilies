# asset_lilies_competitive_strategy

## 1. 核心结论

Lilies 的竞品不是单一 Agent SDK，而是四类系统的交叉区域：

- 可视化 Agent 工作流平台：Dify、n8n、Coze、FastGPT。
- Code-first Agent 编排框架：LangGraph、OpenAI Agents SDK、AutoGen / Microsoft Agent Framework、CrewAI。
- Durable execution / 平台 Harness 基础设施：Temporal 等。
- 垂直智能体与编码智能体：Claude Code、SWE-agent 相关机制。

Lilies 的差异化不能只靠“Builder Prompt 会搭工作流”。更稳定的定位是：可验证、可复用、模型无关的智能工作流工程平台。

## 2. 获得成本

这个资产来自竞品报告、论文综述、本地架构文档和后端核心设计的综合。它把外部产品、研究论文和 Lilies 当前代码能力压缩成后续路线判断。

## 3. 证据链

- `docs/source-materials/2026-07_initial_architecture_research/Lilies_竞品研究论文与未来方向报告.docx`
- `docs/source-materials/2026-07_initial_architecture_research/MEETING_RESPONSE.md`
- `docs/source-materials/2026-07_initial_architecture_research/PAPER_OUTLINE.md`
- `docs/source-materials/2026-07_initial_architecture_research/THE_PRIMITIVE_IS_THE_PAIR_v1_LILIES_CENTRIC_CN.md`

## 4. 适用边界

适用于：

- 判断下一阶段研发优先级。
- 比较 Lilies 与 Dify、n8n、LangGraph、Temporal、Claude Code 的关系。
- 决定是否投入模板市场、Benchmark、Platform Harness、跨框架导入导出。

不适用于：

- 作为竞品绝对排名。
- 代替真实用户研究。
- 代替最新价格、功能或开源协议检查。

## 5. 复用方式

路线优先级：

1. 先补 Platform Harness、任务监控边界、预算/取消/重试/发布门禁可视化。
2. 再建立 Builder benchmark、模板质量评分、真实工具证据和回归测试集。
3. 然后推进 Builder-as-workflow、Workflow-as-tool、跨框架导入导出和模板市场。
4. 长期把 Lilies 定位为可验证、可复用、模型无关的智能工作流工程平台。

评估每个新想法时问：

- 它是否提升 `BlockFlow` 的可验证性？
- 它是否能沉淀为 Template 或 benchmark 数据？
- 它是否降低 Builder 生成成本或提升修复成功率？
- 它是否补齐竞品已有但 Lilies 缺失的 Harness 能力？

## 6. 禁止滥用场景

- 不要用“竞品也有工作流画布”否定 Lilies；关键差异在测试门禁、模板飞轮和 Harness 边界。
- 不要用“论文支持 workflow generation”证明 Builder Team 一定可靠；必须用本地 benchmark 和成本指标验证。
- 不要把多智能体团队当作天然卖点；它需要端到端成功率、成本和真实工具证据支持。
