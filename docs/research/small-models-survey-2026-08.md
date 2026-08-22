# 小参数开放权重模型选型调研报告（面向"harness 模型无关性"论文的多模型消融）

> 调研日期：2026-08-18。核心数据源：BFCL V4 官方榜单原始 CSV（直接从
> gorilla.cs.berkeley.edu 拉取，榜单更新于 2026-04-12）、HuggingFace 官方模型卡、
> 各厂商官方发布页。本文为调研代理产出的原始报告归档，供论文引用与复核。

## 〇、先说结论（TL;DR）

- **主推组合（4 个）**：Qwen3.5 家族（做参数阶梯）+ Gemma 4 + Ministral 3 + GLM-4.7-Flash。四者覆盖中、美、欧三个知名机构谱系，全部 Apache 2.0 / MIT，全部 2025-12 之后发布，工具调用能力有官方或榜单数据支撑。
- **审稿人加分项**：Ai2 OLMo 3（全流程开放，可复现性天花板）作为备选之一强烈建议纳入。
- **关键时效提醒**：2026 年上半年小模型格局大洗牌——Qwen3.5（2026-03 放出 0.8B–9B 小档）、Gemma 4（2026-03-31，**许可证改为 Apache 2.0**）、GLM-4.7-Flash（2026-01，MIT）、Meta Muse Glimmer（2026-08-10，Apache 2.0）。若论文只引用 2025 年的 Qwen3 / Gemma 3 / Phi-4，会显得过时。

## 一、权威基准现状：BFCL V4 官方榜单（唯一可全文核对的官方工具调用榜）

榜单版本 V4（ICML 2025 论文配套），页面标注 Last Updated: **2026-04-12**。官方数据文件 `data_overall.csv`（109 行全量）。小参数开放权重模型摘录（Overall Acc，FC = 原生 function calling 模式）：

| 排名 | 模型 | 参数 | Overall Acc | 许可证 |
|---|---|---|---|---|
| 18 | xLAM-2-32b-fc-r（Salesforce） | 32B | 54.66% | CC-BY-NC-4.0（非商用）|
| 25 | **Nanbeige4-3B-Thinking-2511** | **3B** | **51.40%** | Apache 2.0 |
| 29 | **Qwen3-32B** | 32B | **48.71%** | Apache 2.0 |
| 34 | xLAM-2-8b-fc-r | 8B | 46.68% | CC-BY-NC-4.0 |
| 39 | Qwen3-8B | 8B | 42.57% | Apache 2.0 |
| 40 | ToolACE-2-8B（华为诺亚+中科大） | 8B | 42.44% | Apache 2.0 |
| 41 | Qwen3-30B-A3B-Instruct-2507 | 30B-A3B | 41.39% | Apache 2.0 |
| 43 | Qwen3-14B | 14B | 41.03% | Apache 2.0 |
| 54 | Qwen3-4B-Instruct-2507 | 4B | 35.68% | Apache 2.0 |
| 66 | Gemma-3-12b-it（Prompt 模式） | 12B | 30.43% | Gemma 条款 |
| 70 | Phi-4（Prompt） | 14B | 28.79% | MIT |
| 71 | Qwen3-1.7B | 1.7B | 28.41% | Apache 2.0 |
| 86 | MiniCPM3-4B-FC | 4B | 25.55% | Apache 2.0 |
| 92 | Qwen3-0.6B | 0.6B | 23.93% | Apache 2.0 |
| 98 | Llama-3.2-3B-Instruct | 3B | 21.95% | Llama 社区许可 |
| 107 | Llama-3.2-1B-Instruct | 1B | 10.82% | Llama 社区许可 |

参照系：榜首 Claude-Opus-4.5 为 77.47%，GPT-4.1 为 53.96%，GPT-5-mini 为 55.46%。**即 3B 的 Nanbeige4 和 32B 的 Qwen3 官方成绩已接近 GPT-4.1 档**。
来源：[BFCL 官方榜单](https://gorilla.cs.berkeley.edu/leaderboard.html)（数据文件 `data_overall.csv`，2026-04-12 更新，2026-08-18 抓取）；[BFCL 论文（ICML 2025）](https://proceedings.mlr.press/v267/patil25a.html)。

**重要注意**：该榜尚未收录 2026 年新模型（Qwen3.5、Gemma 4、Ministral 3、GLM-4.7-Flash、Nemotron 3 Nano）。下文这些模型的分数来自各家模型卡自报，harness 不同，**不能与官方榜数字直接横比**——论文里引用时务必分开标注"官方榜实测"与"厂商自报"。

## 二、主推清单（按学术说服力排序）

### 1. Qwen3.5 家族（阿里 Qwen 团队）——参数阶梯主轴，第一推荐

- **规格**：0.8B / 2B / 4B / 9B / 27B（dense）+ 35B-A3B / 122B-A10B（MoE）；小档 2026-03-02 发布，旗舰 2026-02-16。全系 **Apache 2.0**，原生多模态，262K 上下文（可扩至 1M），混合 Gated DeltaNet 架构。
- **工具调用（模型卡自报，含 BFCL-V4 与 TAU2-Bench）**：

| 模型 | BFCL-V4 | TAU2-Bench | IFEval |
|---|---|---|---|
| Qwen3.5-35B-A3B | 67.3 | 81.2 | 91.9 |
| Qwen3.5-9B | 66.1 | 79.1 | 91.5 |
| Qwen3.5-4B | 50.3 | 79.9 | 89.8 |
| Qwen3.5-2B | 43.6 | 48.8 | 78.6 |
| Qwen3.5-0.8B | 25.3 | 11.6 | 52.1 |

  （35B-A3B 卡中同框对照：Claude-Sonnet-4.5 BFCL-V4 72.2 / TAU2 69.8——自报 TAU2 已超 Sonnet。另有 SWE-bench Verified 69.2、Terminal-Bench 2 40.5。）
- **对论文的价值**：同一家族、同一 chat template、同一许可证下覆盖 0.8B→35B 完整阶梯，是做"harness 固定、模型能力变化"消融的理想主轴；且每档都有自报 BFCL/TAU2 基线可引用。
- **部署**：0.8B–4B 单卡 8GB 即可；9B 约 6–8GB（4bit）；35B-A3B 权重 4bit 约 18–20GB 但激活仅 3B，消费级卡可跑。vLLM/SGLang 官方支持。
- 来源：[Qwen3.5-4B 模型卡](https://huggingface.co/Qwen/Qwen3.5-4B)、[Qwen3.5-9B](https://huggingface.co/Qwen/Qwen3.5-9B)、[Qwen3.5-35B-A3B](https://huggingface.co/Qwen/Qwen3.5-35B-A3B)、[Qwen3.5-2B](https://huggingface.co/Qwen/Qwen3.5-2B)、[Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B)、[官方博客 "Qwen3.5: Towards Native Multimodal Agents"](https://qwen.ai/blog?id=qwen3.5)（2026-02/03）。
- **版本注意**：Qwen3.6 已于 2026-04 发布但只有 27B 和 35B-A3B 两档（主打 agentic coding，[GitHub](https://github.com/QwenLM/Qwen3.6)）；小档（≤9B）最新仍是 Qwen3.5。论文若选大档可考虑 Qwen3.6-35B-A3B，小档用 Qwen3.5。

### 2. Gemma 4（Google DeepMind）——机构名望 + 许可证重大转变

- **规格**：E2B（2.3B 有效）/ E4B（4.5B 有效）/ 12B（unified 多模态，2026-06-03）/ 26B-A4B（MoE，3.8B 激活）/ 31B（dense）。首发 **2026-03-31**，256K 上下文，140+ 语言。
- **关键卖点**：**Gemma 4 起改用 Apache 2.0**（Gemma 3 及之前是自定义条款）——这消除了以往论文引用 Gemma 的许可证顾虑；模型卡明确写"native support for structured tool use, enabling agentic workflows"。
- **短板（必须在论文中说明）**：官方模型卡**未给出 BFCL / tau-bench 数字**（给了 MMLU-Pro 82.6、LiveCodeBench v6 77.1 等）；工具调用强度需自测。上一代 Gemma 3 在 BFCL 官方榜表现平平（12B：30.43%），可作为"弱工具调用模型在 harness 下仍能交付"的论证素材。
- 来源：[Gemma 官方发布历史](https://ai.google.dev/gemma/docs/releases)（2026-03-31 / 04-16 / 06-03 条目）、[gemma-4-26B-A4B 模型卡](https://huggingface.co/google/gemma-4-26B-A4B)、[Apache 2.0 许可报道](https://www.ghacks.net/2026/04/06/google-releases-gemma-4-in-four-model-sizes-under-apache-2-0-license/)（2026-04-06）。

### 3. Ministral 3 系列（Mistral AI）——欧洲机构多样性 + 原生 FC

- **规格**：3B / 8B / 14B 三档，各有 base / instruct / reasoning 变体，带图像理解；2025-12-02 随 Mistral 3 发布，**全系 Apache 2.0**，256K 上下文。官方明确"native function calling and JSON output"。
- **说服力**：Mistral 是审稿人熟知的名字；3B/8B/14B 三档与 Qwen 阶梯错位互补。官方博客称 14B reasoning 版 AIME'25 达 85%。
- **短板**：官方公告**未附 function calling 专项基准分**；上一代 Ministral-8B-2410 在 BFCL 官方榜仅 11.10%（且是研究许可证）——新旧两代差异大，引用时别混淆。
- 来源：[Mistral 3 官方公告](https://mistral.ai/news/mistral-3/)（2025-12-02）。另有 Mistral Small 4（约 24B，2026-03-16，Apache 2.0，融合推理/视觉/agentic coding）可作 24B 档备选，但此信息来自二手媒体，**未经官方页核实**。

### 4. GLM-4.7-Flash（智谱 Z.ai）——30B 档 agentic 实测最强的小模型

- **规格**：30B-A3B MoE（总 31B，激活 3B），**MIT 许可证**，2026-01-19 发布，主打消费级硬件本地部署（RTX 3090 / Apple Silicon 实测 43–82 tok/s）。
- **自报成绩**：SWE-bench Verified **59.2**、τ²-Bench **79.5**、BrowseComp 42.8——这是 ≤32B 开放权重里最强的 agentic 组合数字之一。同门 GLM-4.6（355B）在 BFCL 官方榜排第 4（72.38%，MIT），是开放权重榜首，可佐证 GLM 系工具调用血统。
- **对论文的价值**：与 Qwen/Gemma/Mistral 形成第四个独立谱系；MIT 许可证最宽松；vLLM 提供专用 `tool-call-parser glm47`。
- 来源：[zai-org/GLM-4.7-Flash 模型卡](https://huggingface.co/zai-org/GLM-4.7-Flash)、[发布报道](https://www.techloy.com/zhipu-ai-launches-glm-4-7-flash-a-local-ai-coding-model-for-consumer-hardware/)（2026-01）。

## 三、备选清单

**A. OLMo 3 / 3.1（Ai2，7B & 32B）——可复现性王牌**
2025-11-20 发布，Apache 2.0，7B/32B dense，65K 上下文；Instruct 变体专门为 function calling 做了 SFT+DPO+RLVR。独特优势：**数据（Dolma 3/Dolci）、训练代码、中间 checkpoint 全部开放**——如果审稿人质疑"模型是否见过你的工具格式"，只有 OLMo 能让你查训练数据。绝对性能弱于 Qwen3.5 同档（官方对标 Qwen 2.5 / Gemma 3 一代）。2026 年推出了 OLMo-3.1-32B-Instruct 更新版。来源：[Ai2 官方博客](https://allenai.org/blog/olmo3)（2025-11-20）、[Olmo-3.1-32B-Instruct](https://huggingface.co/allenai/Olmo-3.1-32B-Instruct)。

**B. NVIDIA Nemotron 3 Nano 30B-A3B——有官方口径 BFCL 的 30B 档**
2025-12-15 发布，混合 Mamba-2+MoE，激活 3.5B，1M 上下文。模型卡自报 **BFCL v4 53.8、TauBench V2 49.0、IFBench 71.5**。缺点：许可证是 NVIDIA Open Model License（允许商用但非 OSI 标准许可证，说服力略逊 Apache）。来源：[模型卡](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16)。

**C. Salesforce xLAM-2-fc-r（1B/3B/8B/32B）——"FC 专精模型"消融轴**
BFCL 官方榜实测：32B 54.66% / 8B 46.68% / 3B 41.22% / 1B 30.44%，是榜上小模型最强梯队。缺点：**CC-BY-NC-4.0 非商用许可证**；且它是专门为 function calling 微调的"特化模型"——但这恰好可以作为论文的一个对照轴（特化小模型 vs 通用小模型在 harness 下的差异）。来源：BFCL 官方 CSV（同上）、[xLAM 论文](https://arxiv.org/pdf/2409.03215)。

**D.（观察项）Meta Muse Glimmer 30B**
2026-08-10 刚发布，Apache 2.0，30B dense 多模态，明确为本地 agent 工作流优化（Meta 回归宽松许可的标志性发布）。太新，无第三方基准，本次论文引用风险高，但值得在 related work 提一句。来源：[VentureBeat](https://venturebeat.com/technology/meta-returns-to-open-source-with-muse-glimmer-an-apache-2-0-licensed-30b-parameter-ai-model-optimized-for-agents-available-now)、[HF](https://huggingface.co/meta-models/Muse-Glimmer-30B)（2026-08-10）。

## 四、极小档（≤4B）子清单——"很多小模型组成 builder"实验用

| 模型 | 参数 | 机构 | 许可证 | 工具调用依据 | 备注 |
|---|---|---|---|---|---|
| **Qwen3.5-4B** | 4B | 阿里 | Apache 2.0 | 自报 BFCL-V4 50.3 / TAU2 79.9 | 极小档首选，多模态，262K 上下文 |
| **Nanbeige4-3B-Thinking-2511** | 3B | Nanbeige LLM Lab | Apache 2.0 | **BFCL 官方榜实测 51.40%（第 25 名，超 Qwen3-32B）** | 官方榜有据可查的最强 ≤4B；机构知名度低，建议作为"惊喜数据点"而非主轴 |
| **Qwen3.5-2B** | 2B | 阿里 | Apache 2.0 | 自报 BFCL-V4 43.6 | |
| **Gemma 4 E2B / E4B** | 2.3B/4.5B 有效 | Google | Apache 2.0 | 官方声明原生工具调用，无公开分数 | 机构名望最高的极小档 |
| **Ministral 3 3B** | 3B | Mistral | Apache 2.0 | 官方声明原生 FC+JSON，无公开分数 | |
| **Granite 4.0 Micro/Nano** | 3B / 1B / 350M | IBM | Apache 2.0 | IBM 自报 Granite-4.0-1B BFCL**v3** 54.8（同尺寸第一；注意是 v3 口径） | ISO 42001 认证 + 签名权重，企业合规叙事强 |
| **SmolLM3-3B** | 3B | HuggingFace | Apache 2.0 | 支持工具调用；无 BFCL 分数 | 训练配方全公开，复现性好；2025-07-08 发布，截至 2026-08 无 SmolLM4 |
| **Phi-4-mini** | 3.8B | 微软 | MIT | 官方称面向 function calling 设计；无官方榜分数 | 2025-02 发布，已显旧；Phi-4（14B）官方榜仅 28.79% |
| Qwen3-0.6B / 1.7B | 0.6B/1.7B | 阿里 | Apache 2.0 | 官方榜实测 23.93% / 28.41% | 可作"能力下限"锚点 |

参考文献级佐证：TinyLLM（[arXiv 2511.22138](https://arxiv.org/pdf/2511.22138)）系统评测了边缘设备上 SLM 的 agentic 能力（其 harness 下 xLAM-2-3b 65.74%、Qwen3-4B 62.04%、Qwen3-0.6B 45.76%），可直接引用支撑"极小模型工具调用可行性"。

## 五、结构化输出（JSON Schema 遵循）——与论文主张直接相关

- **JSONSchemaBench**（[arXiv 2501.10868](https://arxiv.org/abs/2501.10868)，1 万个真实 schema）：对 Guidance/Outlines/XGrammar/llama.cpp 等约束解码框架的系统评测；关键发现是**复杂 schema 下框架覆盖率从 86% 崩到 3%**——即"引擎级约束解码"本身有覆盖率边界，这正好论证"在工具边界执法（harness 侧校验+重试）"相对"解码器侧约束"的独立价值。
- **"Let Me Speak Freely?"**（[arXiv 2408.02442](https://arxiv.org/pdf/2408.02442)）：格式硬约束可能损伤推理质量——支撑"harness 在边界校验、模型自由生成"的设计。
- BFCL V4 官方 CSV 新增了 **Format Sensitivity（Max Delta / Std）** 列，量化模型对提示格式的敏感度，可作为"弱模型格式脆弱性"的现成引用数据。
- 工程事实：vLLM/SGLang 对上述全部推荐模型支持 guided decoding（schema 级保证），意味着"JSON 合法性"可由 harness 保证而与模型无关——这本身就是论文主张的一个小论据。

## 六、其余候选族群的排除结论（逐一确认过）

- **DeepSeek 蒸馏系**：R1-Distill（1.5B–32B）是 2025-01 的产物，基于 Qwen2.5/Llama3，无原生 FC 训练，工具调用口碑弱；2026 年 DeepSeek 主线（V4 Pro 1.6T / V4 Flash 284B，2026-04-24）全部超出尺寸范围，**没有新的小模型蒸馏版**。不推荐。来源：[DeepSeek-R1 GitHub](https://github.com/deepseek-ai/deepseek-r1)、[BentoML 综述](https://www.bentoml.com/blog/the-complete-guide-to-deepseek-models-from-v3-to-r1-and-beyond)。
- **Llama**：2026 年**没有新的小 dense Llama**；Llama 3.2 1B/3B 在 BFCL 官方榜垫底（10.82%/21.95%），Llama 4 最小的 Scout 也是 109B 总参。除非要一个"弱基线"，否则不推荐（Meta 谱系可用 Muse Glimmer 替代）。
- **Kimi/月之暗面**：K2.6（2026-04-20，1T MoE，修改版 MIT）和 Kimi Linear（48B-A3B）都超出 ≤32B 范围，**无小模型**。排除。来源：[Wikipedia Kimi](https://en.wikipedia.org/wiki/Kimi_(chatbot))。
- **MiniCPM（面壁）**：最新为 MiniCPM4.1-8B（融合思考）与新旗舰 MiniCPM5-1B，Apache 2.0，有 MCP 专用变体（MiniCPM4-MCP）；但榜上成绩弱（MiniCPM3-4B-FC 25.55%），作边缘部署叙事的引用即可。来源：[OpenBMB GitHub](https://github.com/openbmb/minicpm)。
- **LFM2.5-8B-A1B（Liquid AI，2026-05-28）**：端侧工具调用有特色（Pythonic function call），但 **LFM Open License 限制年收入 >$10M 商用**，许可证叙事不干净。仅提一句即可。来源：[官方博客](https://www.liquid.ai/blog/lfm2-5-8b-a1b)。
- **ToolACE-2-8B**（华为诺亚+中科大，Apache 2.0，官方榜 42.44%）：可作 FC 特化对照的许可证友好替身（替代 NC 许可的 xLAM）。

## 七、未找到 / 不确定事项（诚实清单）

1. **Gemma 4 与 Ministral 3 的官方 BFCL/tau-bench 分数**：两家官方材料均未提供，需自测（这反而是论文的贡献点）。
2. **Qwen3.5/3.6、GLM-4.7-Flash、Nemotron 3 Nano 均未上 BFCL 官方榜**（榜停在 2026-04-12），上述分数全是厂商自报，harness 口径不一。
3. **Phi-5**：只有传闻（14B 主档+3-4B mini），微软未官宣；Phi 家族 2026 年最新实为 Phi-4-reasoning-vision-15B（2026-03）。
4. **Mistral Small 4**（2026-03-16）细节仅来自二手媒体，未在 mistral.ai 官方页核实。
5. **tau2-bench 聚合榜**（pricepertoken 等）显示 GLM-4.7-Flash 98.8% 之类数字，疑为 telecom 单域口径，与模型卡 79.5 不一致，**不要引用聚合榜**，引官方 repo（[sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench)）+ 模型卡。
6. **ACEBench** 对 2026 新小模型的覆盖几乎为零，只找到 Qwen2.5 时代数据，不建议作为论文主基准。
7. Nanbeige LLM Lab 的机构背景（据称国内团队，[技术报告 arXiv 2512.06266](https://huggingface.co/Nanbeige/Nanbeige4-3B-Thinking-2511)）知名度存疑，用其数据时建议标注"官方榜可查但机构较新"。

## 八、给实验设计的一句话建议

最能让审稿人信服的配置：**Qwen3.5 阶梯（0.8B → 4B → 9B → 35B-A3B）做主消融**（同族同模板，隔离"参数量"变量），再加 **Gemma 4 E4B/12B + Ministral 3 8B + GLM-4.7-Flash** 做跨族验证（隔离"训练谱系"变量），OLMo 3-7B-Instruct 作可复现性锚点。全组 Apache 2.0/MIT，全部可 vLLM 本地部署、可 OpenRouter/各家 API 复现，两条复现路径都干净。
