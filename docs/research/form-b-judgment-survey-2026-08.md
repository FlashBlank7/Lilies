# 形态B「判断力供给」调研报告（调研代理原文，2026-08-23）

## 1. 固定流程里的 plan-then-execute
- **LLMCompiler**（ICML24，arxiv.org/abs/2312.04511，2023-12）：先生成完整任务DAG再并行执行，vs ReAct 延迟↓3.7×、成本↓6.7×、准确率+~9%。计划=显式图，依赖关系可机械校验。
- **ADaPT**（NAACL24 Findings，abs/2311.05772）：按需递归分解——先试原子执行，失败才调planner分解；ALFWorld/WebShop/TextCraft 绝对+28.3/+27/+33。此逻辑可直接编入状态机（"卡死升级32B"之前多一级"请求分解"）。
- **CodeAct**（ICML24，abs/2402.01030，2024-02）：以可执行Python为统一动作，M3ToolEval成功率最高+20pp、动作数省30%；代码自带控制流，绕开"逐步选积木"的误选面。
- **Agentless**（abs/2407.01489，2024-07）：层级定位→多候选修复采样→回归+复现测试机械择优，SWE-bench Lite 32%/$0.70胜多数agent。启示：选型不求一次命中，采样N个候选让确定性验证器裁决。
- 计划机械校验：**LLM-Modulo**（ICML24 abs/2402.01817）外置critic循环把规划成功率从个位数提到40%+；Google **PlanGEN**（abs/2502.16111，2025-02）约束抽取+验证+算法选择三agent迭代验证。

## 2. 工具选型可靠化
- 语义检索碾压按名选：**ToolLLM**（ICLR24，abs/2307.16789）专训检索器NDCG@5均值**84.9 vs BM25 17.0 / OpenAI-Ada 45.4**；**Gorilla**（NeurIPS24，abs/2305.15334）检索感知微调后API正确率超GPT-4 20.43%并显著降幻觉。
- 通用embedding不够：**ToolRet**（ACL25 Findings，abs/2503.01763）43k工具上最强通用IR模型nDCG@10仅33.8；用其200k专用训练集微调可大幅改善。
- "名字误导"专门研究：**Canary Tools**（abs/2608.04719，2026-08）定义六类陷阱，"语义诱饵（名对功能错）"即 aggregator 案例；模型易感性差36倍。**Looking Is Not Picking**（abs/2606.16364，2026-06）：BFCL失败例中80%注意力已落在正确工具、错在读出层；改描述/重排只救≤23%，改选择机制救59-91%。
- 描述工程：Anthropic 官方指南（anthropic.com/engineering/writing-tools-for-agents，2025-09）：区分性命名、写明"何时不用"、按eval迭代；**EasyTool**（abs/2401.06201）精简+带用例重写降误用；**DRAFT**（ICLR25 Oral）让模型试用工具后自动改写文档，跨模型泛化。

## 3. DSPy
- 原论文（ICLR24，abs/2310.03714）：Llama2-13b管道9%→47%。MIPROv2 第三方：Llama-3.1-8B MMLU 68.3→71.1；社区常见10-40%提升。
- **GEPA**（ICLR26 Oral，abs/2507.19457）：反思式提示进化，平均超GRPO 6-10%（至+20%）且rollout省35×，超MIPROv2 10%+——边界拒绝错误正是现成反思素材。
- DSPy 3.0（2025-06）+MLflow，生产可用。嫁接点：每个状态的"角色提示+投影"=一个签名，以护栏通过率为metric、32B为教师离线编译。

## 4. 案例库/轨迹复用
- **AWM**（abs/2409.07429）：成功轨迹归纳工作流再检索注入，Mind2Web/WebArena相对+24.6%/+51.1%，OOD +8.9~14.0pp。
- **DS-Agent**（ICML24，abs/2402.17453）：低资源CBR改编过去成功方案，one-pass平均+36%。
- **Memento**（abs/2508.16153，2025-08）：案例库+可学习Q检索，GAIA 87.9% Pass@3，OOD +4.7~9.6pp。
- **AFlow**（ICLR25 Oral，abs/2410.10762）：MCTS在代码表示的工作流空间搜索，+5.7%，小模型以GPT-4o 4.55%成本反超。

## 5. 小模型规划专项
- **xLAM-2/APIGen-MT**（abs/2504.03601）：8B BFCL-v3 72.8>GPT-4o 72.1，多轮69.3 vs 47.6；**ToolACE**（ICLR25）8B BFCL 91.4%。
- **PDDL-INSTRUCT**（abs/2509.13351，2025-09）：VAL验证器反馈做逻辑CoT微调，Llama-3-8B计划有效率28%→94%——"机械验证器×小模型微调"与我们边界反馈同构。
- **AgentPRM**（abs/2502.10325）：3B+过程奖励ALFWorld 88.1%>GPT-4o；InversePRM 1万示范86.6%。
- **Agent Distillation**（NeurIPS25，abs/2505.17612）：0.5-3B学到完整agent行为、追平上一档。

## 6. 约束解码新做法
**XGrammar-2 标签触发结构切换**（abs/2601.04426，2026-01）：按已生成内容动态换子语法（选中某工具后切到其参数schema）——"分步约束"已原生化。
