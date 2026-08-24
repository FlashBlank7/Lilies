# 多模型协作调研与小模型 builder 设计选项（2026-08-22）

> 要回答的问题：「一个较强的模型统筹 + 多个小模型按 prompt 做机械性工作」的 builder
> 应该怎么设计。三个并行调研代理（学术论文 / 开源项目 / 可靠性工程）的原始报告
> 全文见附录 A/B/C；本文是面向决策的合成。文中数字均转引自附录报告，出处见原文。

## 1. 现状：我们已有什么

**已落地两套 builder**（注册表并排，按构建选择）：

- `classic`：单模型自由 agent loop（协调者可 spawn 队友，per-actor 模型白名单已落地）。
  即设计文档形态 A 的载体——统筹者换成本地强模型即可重跑，零新代码。
- `mechanical`（今天新落地，形态 B v0）：**无统筹模型**。确定性状态机管三阶段
  （建图 → 验收编写 → 跑测/修复），阶段转移全部机械可判；小模型是无状态提案函数，
  每次调用收到窄角色提示 + 积木目录 + 已查阅 schema 黑板 + 未解决拒绝记录 + 草稿投影
  + 剩余预算，恰好提案一个动作，被硬门拒绝就带错误重试。

**四单真线迭代的实证**（Qwen3-4B，全本地）：

| 单 | 失败形态 | 换来的机制 |
| --- | --- | --- |
| 1 | 幻觉积木类型 + 被拒提案原地重试 17 轮 | 目录进上下文；反刍守卫（同一被拒提案 3 次判停） |
| 2 | 成功地重复查询 18 轮零进展 | 黑板记忆（查过的 schema 常驻）；只读循环守卫（8 次判停） |
| 3/4 | 同一段残 JSON 逐字节重犯 | 被拒记录常驻黑板；报错附模型自己的原文 |

**关键翻案**：判别实验证明"4B 拍平嵌套 JSON / 残 JSON"是 **vLLM 0.8.5 hermes
流式工具解析器丢参数片段**（非流式直连输出完美；流式拼装 141/150 字符、
错误位置与平台所见逐字节一致）。自冒烟测试以来所有"小模型 schema 失败"
均为错误归因，4B 真实能力被系统性低估；历史测量需在管道修复后重新标定。

## 2. 调研合成：八个设计决定

1. **架构范式有最同构的文献支撑**：Stanford MinionS（强模型把任务切碎成
   "短上下文 + 单步指令"的任务卡下发给 1–8B 本地模型）——省 5.7 倍成本保 97.9%
   质量；自由对话式协议只保 87%。Anthropic 编排者-工作者内部评测 +90.2%
   （代价 ~15 倍 token）。NVIDIA 立场文同主张"LLM 规划 + SLM 执行"。
   **结论：小模型的可靠性来自统筹者把任务切碎到机械粒度，不来自小模型自主规划。**

2. **机械执行 4B 就够，差距全在多轮**：Qwen3 技术报告 BFCL v3 单轮：4B 65.9 /
   8B 68.1 / 30B-A3B 69.1 / 235B 70.8——随规模近乎平坦；多轮断崖
   （30B-A3B multi-turn 仅 7.9%）。**结论：执行者用 4B 档；编排层必须把工作
   展平为单轮填空，绝不让小模型维护多轮状态。**

3. **统筹者规模底线 ≥32B 档**：τ²-bench 上 30B-A3B 当多轮统筹明显不够、
   32B dense 勉强及格；HF Open Deep Research 显示统筹者能力≈系统上限。
   候选：Qwen3-32B（保守稳妥）、GLM-4.7-Flash 30B-A3B（自报 τ² 79.5 /
   SWE-bench 59.2，需自测且要新 vLLM + glm47 parser）。四张 48G 卡跑得动任一档。

4. **约束解码是白捡的硬门**：vLLM 内置 xgrammar（新版统一为
   `structured_outputs` 参数），复杂 schema 正确率 +20~25 个百分点、开销近零、
   schema 固定可缓存（我们的积木 schema 正是最佳场景）。`tool_choice=named/required`
   或 strict:true 才走约束，auto 默认不约束。**结论：4B 执行者全量开启——
   语法错误归零，硬门与重试预算专职拦语义错误。**

5. **升级阶梯的前提我们天然满足**：级联文献共同结论——级联优于单发路由的前提是
   "失败可检测"；我们的 Pydantic/测试硬门是零成本确定性验证器，正是 2026 年
   级联论文假设里最理想的情形。协议抄 CAMEL Workforce：RETRY（执行者带错误重试）
   → REPLAN（统筹者改写任务卡）→ 升级（统筹者亲自干/更大模型），失败 N 次优雅停机。
   注意：机械格式类失败留在重试层，升级只留给理解类失败。

6. **任务卡格式（统筹者→执行者的唯一接口）**：四要素——目标、输出格式（固定
   schema）、工具指引、任务边界（Anthropic 报告：缺一则子代理重复劳动或漏活）；
   加 1 个示例（few-shot 对小模型 +5~8%，1-shot 收益最大）；上下文压到几 K
   （Context Rot：上下文越长小模型指令遵循非均匀下滑）；显式带全部所需字段而非引用。
   消息面可抄 MinionS 的 JobManifest/JobOutput（强制 citation + 可空 answer 抑制幻觉）。

7. **反面清单（文献里的坑）**：弱模型不放"平行意见贡献者"位置（Self-MoA：混入
   弱模型拉低质量）；不共享全量历史（传话损耗；对策是任务卡自足）；统筹者不盲信
   下属产出（MAST 1600+ 轨迹：验证类失败占 24.5%；结构性验证门 +9.4~15.6%，
   提示词补丁天花板低）——我们的硬门路线与此完全一致。生成与验证角色分离
   （AgentCoder/Co-PatcheR）：测试断言不交给写图的同一个模型。

8. **基建修复路径确认**：我们踩的流式 tool-call 丢片段有一族上游 issue
   （#19056：`"}` 单 token 被截断丢 `}`，与我们现场完全吻合），修复合入
   **vLLM v0.11.0**（PR #25281）。两条路：升级 vLLM ≥0.11（驱动 CUDA 12.4
   兼容性需实测）；或 provider 对本地端点改走非流式（已验证输出完美，构建是
   后台任务不需要流式）。建议双管齐下：先非流式立刻解锁，升级另行验证。

## 3. 设计选项

| | 选项 1：形态 A'（强统筹自由环） | 选项 2：形态 B v0（纯状态机，现状） | 选项 3：形态 B v2（状态机 × 统筹者，推荐） |
| --- | --- | --- | --- |
| 统筹者 | 本地 32B 跑 classic 自由 agent loop | 无（程序即统筹） | 本地 32B，只做**阶段内**分解与兜底 |
| 阶段控制 | 模型自由决定 | 状态机 | **状态机**（保留全部机械关卡） |
| 执行者 | spawn 的 4B 队友 | 4B 单步提案 | 4B 按任务卡单步填空 + 约束解码 |
| 新代码量 | ≈0（已落地，换模型即可） | 0 | 中（任务卡协议 + 统筹者调用点） |
| 风险 | 多轮统筹正是 ≤32B 弱项（τ² 数据）；回合慢 | 判断类工作（选积木、定断言）压给 4B，复杂需求预计顶不住 | 需要新协议设计，但每个部件都有文献数字背书 |
| 论文角色 | 对照臂 | 下界臂（"零统筹"消融） | 主实验臂 |

**推荐选项 3**，即你说的「大点的模型统筹、小的做机械工作」的严格实现：

```text
状态机（代码，管阶段与关卡，零模型自由度）
 ├─ 建图阶段
 │   ├─ 统筹者(32B)：读需求+目录 → 产出结构化任务卡序列
 │   │   （每张：目标节点、积木类型、配置意图、schema、1 示例、边界）
 │   ├─ 执行者(4B×N)：每张卡一次单轮填空（约束解码保证合法 JSON）→ 硬门
 │   └─ 失败协议：4B 带错重试×2 → 统筹者改写卡 → 统筹者亲自配
 ├─ 验收阶段：统筹者写断言（测试作者不下放——生成/验证分离），锚定数字
 └─ 跑测/修复：关卡机械执行；诊断先派 4B 读台账，失败升级统筹者
```

三臂全部保留即论文的完整消融设计：A'（模型管一切）/ B v0（代码管一切）/
B v2（代码管流程、强模型管分解、小模型管执行）。

## 4. 先决基建（不管选哪个都要做）

1. 本地模型调用改非流式（provider 小改，立刻解除丢片段 bug）；
2. `structured_outputs`/strict 工具约束接入 openai_chat provider；
3. 第二个 vLLM 实例拉起统筹者模型（Qwen3-32B 先行，GLM-4.7-Flash 作自测候选）；
4. 修正历史记录中"拍平是 4B 预期失败模式"的错误归因（冒烟脚本注释、设计文档）；
5. （并行验证）vLLM 升级 ≥0.11.0 在本机驱动下的可行性。

## 附录 A：学术论文调研原文（代理报告，2026-08-22）

（以下为调研代理原文，出处与时间标注见文内）

面向背景：强模型统筹 + 几 B 小模型机械执行 + 硬门（schema 校验/乐观锁/预算守卫）+ 廉价重试。Sakana 系（TRINITY/Conductor/AB-MCTS 等）已有专项调研，本文不赘述。

### 一、大小模型协作/分工

**Minions / MinionS**（Stanford Hazy Research，arXiv:2502.15964，2025-02，ICML 2025）：云端强模型统筹、本地小模型读文档执行。自由对话式 Minion 协议省 30.4 倍云端成本但只保住 87% 性能——小模型的失败主因是跟不上多步指令、驾驭不了长上下文；MinionS 协议改为由强模型把任务分解成"短块上下文+单步指令"的子任务并行下发，省 5.7 倍成本保 97.9% 性能。后续：Secure Minions（2025-05，TEE 端到端加密）；2026-05 团队发布两年回顾并转向 OpenJarvis 平台。借鉴：与我们架构最同构的学术证据——小模型可靠性来自"强模型把任务切碎到机械粒度"，而非小模型自主规划；子任务并行+廉价重采样是性价比来源。

**Chain of Agents**（Google+PSU，NeurIPS 2024，arXiv:2406.02818）：worker 代理顺序处理文本分块、逐级传递"通信单元"，manager 汇总，长上下文任务全面超过 RAG 与长窗口基线。借鉴：代理间交接物要标准化，不传原始上下文。

**Mixture-of-Agents**（Together AI，arXiv:2406.04692，2024-06）：分层聚合多个开源模型输出，AlpacaEval 2.0 达 65.1%（GPT-4o 57.5%）。但 **Self-MoA**（arXiv:2502.00674，2025-02）反证：只聚合单一最强模型的多次采样反超混合 MoA 6.6%——混入弱模型会拉低平均质量。借鉴：弱模型不要放在"平行意见贡献者"位置，要放在"输出可被硬门校验"的执行者位置。

**α-UMi**（arXiv:2401.07324，2024-01，EMNLP 2024）：把工具代理拆为 planner/caller/summarizer 三个小模型，先全量微调再按角色继续微调，多个工具基准上超过单体大模型方案——7B 级模型"按角色专精"可胜任分工的直接证明。

### 二、级联与路由

- **FrugalGPT**（arXiv:2305.05176，2023-05）：级联+答案打分器，可省 98% 成本匹配 GPT-4。
- **RouteLLM**（LMSYS，arXiv:2406.18665，2024-06）：偏好数据训练强/弱路由器，保 95% GPT-4 质量、成本降 3.66 倍，路由开销 <0.4%。
- **AutoMix**（NeurIPS 2024）：小模型先答、few-shot 自我验证决定是否升级——但自报置信度普遍校准差。
- 2025–26：**CP-Router**（共形预测控制升级）；**Is Escalation Worth It?**（arXiv:2605.06350，2026-05）：级联收益取决于"置信分与难度的相关性"；**Cluster-Route-Escalate**（arXiv:2606.27457，2026-06）：聚类→派最省模型→质量估计不过关再升级，保 97–99% 准确。

借鉴：级联优于单次路由的前提是"失败可检测"。我们的 Pydantic 校验/测试硬门是零成本、确定性的验证器——按"小模型 N 次重试仍被拒→升级强模型"做阶梯，比任何置信度估计都可靠。

### 三、多智能体软件工程

- **MetaGPT**（ICLR 2024 oral）：SOP 编码进角色，代理间交接结构化文档而非聊天，HumanEval 85.9%。
- **ChatDev**（ACL 2024）：瀑布阶段+两两对话链，均价约 $0.30/软件。
- **AgentCoder**（arXiv:2312.13010）：programmer / test designer / test executor 三角色，测试设计者独立于写码者以防自证，GPT-4 底座 HumanEval 96.3%。
- **MapCoder**（ACL 2024）：检索/规划/编码/调试四代理，HumanEval 93.9%。
- **Agentless**（arXiv:2407.01489，2024-07）：反方证据——固定三段流水线（定位→修复→验证）以 $0.70/issue 胜过多数自由代理。
- **Co-PatcheR**（NeurIPS 2025，arXiv:2505.18955）：3 个 14B 专精小模型分任"定位+生成""复现测试""评审+多数投票验证"，SWE-bench Verified 46%，较 SWE-RL 少 40% 参数。

借鉴：①生成与验证角色必须分离，验证角色恰是弱模型最先胜任的岗位；②固定流程+结构化交接物稳于自由协作，与我们受控工具边界一致。

### 四、弱模型执行可靠性

- **BFCL**（V3 多轮 2024-09、V4 agentic；榜单更新至 2026-04）：小模型单轮按 schema 调用已可用，多轮/agentic 断崖。TinyLLM 自测（arXiv:2511.22138，2025-11）：xLAM-2-3b 总分 65.7%（multi-turn 55.6%）；Qwen3-4B 总分 62.0% 但 multi-turn 仅 35.3%。
- **专精微调可补齐单轮**：ToolACE-8B（ICLR 2025）合成数据微调 8B 达 BFCL-v1 91.41%；Hammer-7B（函数名掩码抗扰动）同规模 SOTA。
- **约束解码**：JSONSchemaBench（arXiv:2501.10868，2025-01）：语法约束可保证合法输出且比无约束快约 50%；XGrammar（arXiv:2411.15100）已内置 vLLM/SGLang。
- 争论：Let Me Speak Freely?（2024-08）称格式约束损害推理；dottxt 复测指其实验混同 JSON-mode 与约束解码，对齐后持平或更好。

借鉴：①grammar/guided JSON 把"结构合法"做到 100%；②多轮是小模型死穴——把节点配置展平为单轮填空；③自托管小模型按角色 SFT（ToolACE/Hammer 配方）投入产出比已被反复验证。

### 五、2025–2026 异构编排新工作

**MasRouter**（arXiv:2502.11133）：级联控制器统一决定协作模式→角色→每角色模型；**HALO**（arXiv:2505.13516）：三层+MCTS 搜索工作流；**AgentOrchestra**（arXiv:2506.12508）：中央规划+专精子代理；**DAAO**（arXiv:2509.11079，WWW 2026）：按查询难度组装工作流深度并路由。工业佐证：Anthropic 多代理博客（2025-06）：Opus 统筹+Sonnet 子代理较单 Opus +90.2%，代价约 15 倍 token。

未找到与"强统筹+几 B 模型按 schema 填工作流节点配置"完全同构的论文——最接近的拼图是 MinionS（任务切碎协议）+ Co-PatcheR（弱模型验证角色）+ JSONSchemaBench（约束解码选型），组合即我们架构的文献支撑。

## 附录 B：开源项目调研原文（代理报告，2026-08-22）

（star 数与版本截至 2026-08-22）

**AutoGen → Microsoft Agent Framework**：microsoft/autogen（60.6k★）2025-10 起维护模式；后继 agent-framework（13k★，MIT，2026-04 GA）：图式 Workflow、群聊/handoff、checkpoint、人工审批、原生 MCP/A2A。勿再基于 AutoGen 立项。

**AG2**：ag2ai/ag2（4.9k★，Apache-2.0，v1.0.2）："协议驱动"重写；每 agent 独立 llm_config，OpenAI 兼容 base_url 即接 vLLM。值得抄：把"谁下一个发言"抽象成 Pattern。

**MetaGPT**：（69.9k★，MIT）OSS 实质停更，团队转商业产品。值得抄：角色间用强 schema 的结构化产物通信。仅思想参考。

**CAMEL**：（17.6k★，Apache-2.0，活跃）：RolePlaying + Workforce 层级（coordinator+planner+workers），显式支持 vLLM/Ollama。值得抄：**Workforce 失败协议——RETRY→REPLAN（改写子任务）→动态增派，失败 3 次优雅停机**。

**LangGraph**：（40.2k★，MIT，1.0 GA 2025-10）：StateGraph/子图、Send（map-reduce 扇出）、Command（goto+状态更新）；官方 supervisor/swarm 预制；每节点任意模型。值得抄：Send 扇出 + checkpointer 中断恢复。坑：supervisor 中转放大 token。

**OpenAI Agents SDK**（28.9k★，MIT）：handoff=名为 transfer_to_<agent> 的工具调用；`handoff()` 支持 input_type（交接参数 schema 化）、input_filter（裁剪移交历史）；agents-as-tools 模式统筹者不失控。值得抄：schema 化交接+历史裁剪。

**Claude Agent SDK**（8k★，MIT）：subagent=AgentDefinition{prompt, tools, model 逐个覆盖, maxTurns}；上下文隔离、仅回传最终消息、默认并行；硬护栏：并发上限、嵌套深度、预算上限；对子代理输出扫描"指令伪装"（注入防御，值得抄进工具边界）。官方博客：orchestrator-worker +90.2%、约 15 倍 token；显式教统筹者委派（目标/格式/工具/边界）；只有可并行的读密集任务值得多 agent。SDK 绑 Claude，只抄设计。

**Stanford Minions**（HazyResearch/minions，1.3k★，MIT，研究级）：MinionS 消息格式——supervisor 生成并执行 prepare_jobs 代码产出 JobManifest{chunk, task, advice}；worker 按 chunk×task 并行，结构化输出 JobOutput{explanation, citation, answer|null}（null=无关供过滤）。值得抄：①以"生成代码"表达分解，天然可 schema 校验；②强制 citation+可空 answer 抑制小模型幻觉。

**Sakana TreeQuest**（0.6k★，Apache-2.0，v0.3.2）：`algo.step(tree, {"modelA": fn,...})`，fn(parent_state)→(state, score)，不同动作名映射不同模型即多模型 AB-MCTS。离线实验组件。

**路由新势力**：RouteLLM（5.4k★，停更，思想可抄）；**vLLM Semantic Router**（5.2k★，Apache-2.0，极活跃）：ModernBERT 级小分类器按意图/复杂度选模型，路由决策零 LLM 调用；**archgw/Plano**（7k★）：自研 4B 编排小模型做路由+函数调用。基建：LiteLLM（57k★）统一网关；多 vLLM 实例用 production-stack / llm-d。

**vLLM 现状**（v0.27.1，2026-08-11）：单实例仍单模型，多模型=多实例+前置路由；Sleep Mode 正式化（休眠/唤醒切换较冷启动快 18-200×）；`guided_json` 等旧参数废弃，统一为 `structured_outputs`（后端 xgrammar/guidance 自动选）；tool_choice 强制走该约束。**hermes 流式丢片段**：上游 issue #19056（`"}` 单 token 被截断丢 `}`）、#21360（`[{"` 被吞）；实际修复为 PR #25281 + #22002，**首个含修复的发行版 v0.11.0（2025-10-02）**；2026-03 另修 stream 间隔>1 丢字段（#38168）。至少升 0.11.0，建议直上 0.2x。

## 附录 C：可靠性工程调研原文（代理报告，2026-08-22）

### 任务下发格式

MinionS 关键消融：复合指令拆成"单步指令×文档小块"，小模型表现提升 56 分；上下文 <1K→>65K，简单抽取任务掉 13%；每 job 固定 JSON（explanation/citation/answer）。Anthropic：任务卡四要素——目标、输出格式、工具指引、任务边界，缺一则重复劳动或漏活。Few-shot 对小模型收益显著大于大模型（Alpaca +8.1% vs GPT-3.5 +1.2%），1-shot 收益最大。Chroma "Context Rot"（2025-07，含 Qwen3 系）：输入越长性能非均匀下滑。结论：执行者收"最小投影上下文 + 单步任务卡 + schema + 1–3 示例"。

### 约束解码

SqueezeBits 实测（2025-09，H100，Qwen3-8B/32B）：复杂 schema 无约束正确率可低至 61.1%，guided decoding +20–25 个绝对百分点；xgrammar 每 token 开销近零，schema 编译 20–50ms 可缓存；vLLM batch≥8 吞吐下降较明显；超复杂 schema xgrammar 可能超时，llguidance 更稳。兼容性坑：`tool_choice=named/required` 走结构化解码；**auto 默认不约束**，需 tool 声明 strict:true（配 VLLM_ENFORCE_STRICT_TOOL_CALLING）。Qwen3 thinking 可与结构化输出共存（--reasoning-parser qwen3）。机械填表放心约束；需推理的让 think 段自由、正文再约束。

### 升级阶梯实证

EcoAssistant：GPT-3.5→GPT-4 级联+代码执行重试，成功率超纯 GPT-4 10%、成本省 50%。CRE（2026-06）：Qwen3-4B 一级 + 30B 二级 + BERT 升级判别器，59% 请求停在一级，精度差 0.7–2.1 分。**Qwen3 技术报告（BFCL v3 thinking）：4B 65.9 / 8B 68.1 / 14B 70.4 / 30B-A3B 69.1 / 235B 70.8——单轮工具调用随规模近乎平坦**；差距在多轮（MUA-RL：30B-A3B multi-turn 7.9%、32B 19.6% vs GPT-4.1 40.5%）。启示：机械格式类失败先廉价重试，升级只留给理解类失败。MinionS 局部模型消融：3B 恢复 93.4%、8B 恢复 97.9%。

### 统筹者规模底线

τ²-bench：Qwen3-30B-A3B = 31.6/18.0/18.4，Qwen3-32B = 50.2/23.5/24.8，GPT-4.1 = 70.2/53.0/38.9。8B/30B-A3B 当多轮统筹明显不够，32B dense 勉强及格。GLM-4.7-Flash（30B-A3B）自报 τ²-Bench 79.5、SWE-bench Verified 59.2——若属实是本地统筹者最强候选，厂商数据需自测（vLLM main + --tool-call-parser glm47 --reasoning-parser glm45）。HF Open Deep Research：统筹者能力≈系统上限。7B/14B 当统筹者的正式对照实验未找到；间接证据均指向 ≥32B dense 或强训练 MoE。

### 已知失败模式（MAST，NeurIPS'25，1600+ 轨迹，14 种失败模式）

规范/设计类 43.9%（违背任务规范 11.8%、步骤重复 15.7%、不知终止 12.4%）；代理间错位 31.95%（推理-行动不一致 13.2%、该问澄清不问 6.8%）；验证类 24.5%（不验证/验证不全 8.2%、错误验证 9.1%）。干预实测：给终审者最终决定权 +9.4%，高层目标校验最高 +15.6%——提示词补丁天花板低，结构性验证门才是正解。传话损耗（Cognition 2025-06）：子代理看不见彼此行动携带的隐含决策；对策是任务卡自足或单线程+压缩。统筹者盲信下属：外置确定性校验优于 LLM 自查。

### Qwen3 工具调用在 vLLM 的坑

官方姿势：vLLM≥0.8.5，--enable-auto-tool-choice --tool-call-parser hermes；官方原话承认"存在 tool call 格式坏解析不了的角落，生产环境建议自行再解析"。2507/Coder 系列改用 qwen3_coder/qwen3_xml parser。配置坑实测：忘加 parser 旗标，工具调用成功率 92.6%→11.18%（mcp-bench issue#12）。已知 bug：hermes JSONDecodeError（0.8.5，#17790）；流式 tool-call 解析错（#19056、#21544）、流式返回原文不解析（#31871）。建议：tool call 走非流式；升最新 vLLM；parser 输出再过一层自校验。thinking 对工具调用 +8–10 分（4B：65.9 vs 57.6）：纯填表执行者可关思考省延迟，写测试断言建议开。
