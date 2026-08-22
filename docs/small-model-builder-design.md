# 小模型集群 Builder 框架设计（草案 v0.1）

> 目标：在现有 Lilies harness 下，用一组几 B 到几十 B 的小模型替代单一大模型驱动构建，
> 验证论文核心主张——**可信构建来自边界执法的 harness，而非模型能力**。
> 若弱模型集群在同一 harness 下仍能交付通过盲测的工作流，主张成立且强化。
>
> 状态：v0.2。三份调研已回填（原始报告归档于 [research/](research/)）。

## 1. 核心命题与设计哲学

单一大模型 builder 的能力来自模型内部：规划、schema 记忆、纪律遵循混在一个上下文里。
小模型集群 builder 把这三者拆开：

- **控制流归代码**：阶段推进（规划→搭建→测试→修复→发布）由确定性状态机驱动，不再依赖模型自觉；
- **纪律归边界**：schema 校验、revision 乐观锁、预算守卫本来就在工具边界执法（`builder.py`），
  小模型的高错误率被边界转化为廉价的重试信号，而不是交付事故；
- **模型只做局部判断**：每个小模型在窄角色、窄工具面、窄上下文里做一件事。

一句话：**大模型 builder 靠模型的自律，小模型集群 builder 靠 harness 的他律。**
这恰好是论文最想证明的对照。

## 2. 平台现状：多模型就绪度盘点（已核实）

| 层 | 现状 | 结论 |
| --- | --- | --- |
| Provider 层 | `MultiProvider` 按 `provider/model-id` 前缀路由（`providers/multi.py:122`），base URL 走环境变量（`DEEPSEEK_BASE_URL`/`OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`） | ✅ 已就绪：本地 vLLM/Ollama 起 OpenAI 兼容端点即插即用，无需新 provider |
| 工作流运行时 | model_turn 积木、Agent spec 已支持 per-node 指定 `model="provider/id"` | ✅ 已就绪 |
| Builder | `WorkflowBuilder.__init__` 收单一 `generator_model`（`builder.py:353`），协调者与所有队友共用（`builder.py:936-937`） | ❌ 唯一瓶颈 |
| 血缘记录 | 每轮 turn 元数据已记录 `model`（`builder.py:914`） | ✅ 实验血缘现成 |
| 队友机制 | `spawn_teammate` 动态角色 + 回合 clamp + 预算守卫（`builder.py:1786-1825, 2153-2177`） | ✅ 结构现成，缺 per-actor model |

## 3. 框架结构：三档递进的实验形态

三档共用同一 harness、同一任务集，本身就是论文的实验臂设计。

### 形态 A：异构队友（最小改造，先跑通）

保持 LLM 协调者，但协调者与队友用不同模型。角色划分依据 32 个真实构建
（1104 轮、1921 次工具调用）的分布数据修正（2026-08-18，初版四角色砍成一个半）：

```text
协调者（最强档）：需求理解、架构选型、测试设计、交付说明 —— 判断类，无硬门兜底
 └── 配置手（~4-7B）：查手册 + 配节点 + 接线（catalog_get/manual_get +
     draft_add_node/update/connect），≈60% 调用量 —— 机械类，硬门全覆盖
（修复诊断：无常驻角色 —— run_inspect 在 32 个构建里仅 16 次；
 小模型先诊，失败升级大模型，结局由 test_run 硬验证）
```

**下放原则（数据驱动的核心结论，直接进论文）**：
可下放度 = 硬门覆盖度 × 调用频次。

- **配置手是唯一"必设"角色**：草稿变更占总调用 38%，加上配套查阅共约 60%；
  本质是"读 schema → 填表"的结构翻译，Pydantic/端口/revision 硬门全覆盖，
  配错即大声拒绝 + 廉价重试——BFCL 直接度量的能力，正是论文命题的落点。
- **查询与使用不拆开**：catalog_get 单项占 15%，但其结果是给配节点的人看的，
  独立"目录侦察员"多一跳传话、摘要损耗变配置错误；且选错积木是**软失败**
  （无硬门拦架构判断），恰恰不能交给弱模型。侦察员角色取消，查阅并入配置手。
- **测试作者不下放**：决定断言什么（判断）+ 算期望值（算术）+ not_contains
  安全断言（对抗思维），是全系统唯一无硬门保护的高危输出——期望值算错且
  恰好吻合错误实现 → 测试绿、交付垃圾、断言冻结。期望值计算交给沙盒
  python（确定性），断言设计留在协调者。
- **修复诊断走升级阶梯**：频次最低（<1%）但认知最难；结局有 test_run 硬验证，
  过程烧修复预算——小模型先试、失败即升级，本身就是一条成本曲线实验。

改造点（✅ 2026-08-18 全部落地，见 §8）：

1. `_agent_loop` 加 `model: str | None` 参数，None 回落 `self.generator_model`；
2. `spawn_teammate` 工具 schema 加可选 `model` 字段，enum 白名单 + 越界硬门；
3. `BuildRequest` 加 `coordinator_model` 与 `teammate_models`（存 team_state，
   进配置指纹，接续 harness-config-ablation.md 的改造路径）。

### 形态 B：机械协调者 + 小模型执行体（论文主实验）

把协调者从 LLM 换成确定性状态机——阶段推进、预算分配、角色调度全是代码，
小模型只在每个阶段内做局部决策：

```text
状态机（代码）：plan → scaffold → wire → test → repair(≤N) → publish
   每阶段：选角色 → 组装窄上下文 → 调小模型 → 边界校验 → 落库或重试
```

- 阶段转移条件全部机械可判：`draft_validate` 通过才进 test，`test_run` 全绿才进 publish，
  修复循环耗尽即失败——这些守卫在 harness 里已存在，状态机只是把"什么时候做什么"也接管；
- 小模型单次调用是无状态的"提案函数"：输入（阶段上下文 + 草稿投影 + 错误反馈），
  输出（一个工具调用提案）。提案被边界拒绝→带结构化错误重试（廉价，几 B 模型 token 便宜）；
- 这是"harness 与模型无关"的最强形式：**控制流里没有任何模型自由度**。

### 形态 C：提案竞争（可选加强，接 §6 外部模式）

schema 密集的节点配置步骤，N 个极小模型（≤4B）并行提案，机械仲裁：
先过 `NodeSpec.model_validate`，再过 `draft_validate`，第一个全过的落库；
全败则升级到更大档模型（升级阶梯：4B → 7B → 30B → 大模型兜底）。
仲裁不需要 LLM——校验器就是裁判，这与平台"验收由确定性机制执法"的哲学一致。

## 4. 小模型的失败模式与 harness 的对应防线

| 小模型典型失败 | 边界防线（已存在） | 需新增 |
| --- | --- | --- |
| JSON/schema 畸形 | Pydantic 校验拒绝（`builder.py:1592-1623`） | 结构化错误反馈进重试 prompt |
| 幻觉字段/积木名 | 积木注册表查找失败即报错 | 无 |
| 忘记上下文、改错对象 | revision 乐观锁拒绝过期操作 | 草稿投影：每次调用重新组装最小上下文 |
| 原地打转 | 修复循环预算 + 停滞守卫 | per-角色重试上限（比全局守卫更细） |
| 术语泄漏进业主文本 | ask_owner 硬门（`builder.py:1232`） | 叙述/交付说明出口硬门（缺口，见消融文档） |
| 上下文窗口小 | `TOOL_RESULT_HISTORY_MAX_CHARS` 截断 | 角色级上下文投影（只给本角色相关的草稿切片） |

关键新增机制是**角色级上下文投影**：小模型不看完整构建历史，每次调用收到的是
为该角色定制的草稿视图（如节点配置员只看目标节点的 schema + 上游输出契约 + 本次任务），
这同时解决窗口限制和注意力分散。draft 状态本来就是唯一真相源（黑板模式），
投影只是查询它的不同视角。

## 5. 模型选型（依据 [research/small-models-survey-2026-08.md](research/small-models-survey-2026-08.md)）

原则：

- 选型必须让审稿人信服：知名机构、开放权重、许可证友好、可本地复现；
- 需要三档：极小档（≤4B，形态 C 提案池）、执行档（~7-14B，队友角色）、
  协调档（~30B，形态 A 协调者）；
- 工具调用与结构化输出基准（BFCL 等）是首要筛选指标，通用能力榜单是次要的。

选定配置（全组 Apache 2.0/MIT，均可 vLLM 本地部署 + API 复现两条路径）：

| 档位 | 主选 | 依据 | 角色 |
| --- | --- | --- | --- |
| 主消融阶梯 | **Qwen3.5 家族 0.8B → 2B → 4B → 9B → 35B-A3B** | 同族同模板隔离"参数量"变量；每档有自报 BFCL-V4/TAU2 基线（4B: 50.3/79.9；9B: 66.1/79.1） | 全形态 |
| 跨族验证 | **Gemma 4 E4B/12B**、**Ministral 3 8B**、**GLM-4.7-Flash（30B-A3B）** | 隔离"训练谱系"变量；Google/Mistral/智谱三个独立谱系；GLM-4.7-Flash 自报 SWE-bench 59.2 / τ²-Bench 79.5，30B 档 agentic 最强 | 形态 A/B |
| 可复现锚点 | **OLMo 3-7B-Instruct**（Ai2） | 数据/代码/checkpoint 全开放，可回查训练数据是否见过工具格式 | 审稿人质询防线 |
| 能力下限锚 | Qwen3-0.6B / 1.7B（BFCL 官方榜实测 23.93%/28.41%） | 官方榜可查的最弱档 | 形态 C 下限实验 |
| 惊喜数据点 | Nanbeige4-3B-Thinking（BFCL 官方榜实测 51.40%，超 Qwen3-32B） | 官方榜可查的最强 ≤4B，但机构较新，不作主轴 | 形态 C |

引用纪律（来自调研报告的硬提醒）：

- BFCL V4 官方榜（2026-04-12 更新）未收录 2026 年新模型；Qwen3.5/Gemma 4/Ministral 3/
  GLM-4.7 的分数全是**厂商自报**，与官方榜数字不可横比，论文中必须分开标注；
- Gemma 4 与 Ministral 3 官方未给 BFCL/tau-bench 分数——我们的 harness 实测本身就是贡献点；
- JSONSchemaBench（arXiv:2501.10868）发现约束解码框架在复杂 schema 下覆盖率从 86% 崩到 3%，
  可直接引用来论证"边界校验+重试"相对"解码器侧约束"的独立价值。

## 6. 外部借鉴模式

已确认的 DeepSeek 线结论（详见 [research/deepseek-harness-survey-2026-08.md](research/deepseek-harness-survey-2026-08.md)）：

- DeepSeek 官方 harness **dsh**（2026-08-13 开源，MIT）的 Minimal 模式只有
  "持久 bash + 字符串替换编辑器"，官方跑基准即用此模式——工具面最小化的执法哲学
  与本平台同构，可对照其工具 schema 校准我们的工具边界；
- 经由 DeepSeek API 走 thinking 模式的工具调用链，中间轮 `reasoning_content`
  必须原样回传，否则 400——形态 A/B 的多轮循环实现时的合规红线；
- DeepSeek strict 模式仍有畸形 JSON 已知问题，不可作为唯一防线——
  恰好佐证"出参校验必须在自己边界做"的设计；
- 2026-08-16 起 DeepSeek API 峰谷计费（价差约 2 倍）：批量实验安排在谷时跑，
  预算守卫值得加时段感知。

### Sakana AI 线结论（详见 [research/sakana-ai-survey-2026-08.md](research/sakana-ai-survey-2026-08.md)）

Sakana 的多模型协作路线到 2026-08 已从算法（AB-MCTS）演进到学习型编排产品（Fugu），
对本设计最有用的五条：

1. **AB-MCTS / TreeQuest**（arXiv:2503.04412，Apache 2.0 库）：把"再采样一个新解（宽）/
   修正现有解（深）/ 换哪个模型"三个决策都交给 Thompson 采样，对每个模型维护质量后验，
   **任务内在线学出"这道题谁擅长"，零训练成本**。唯一硬前提是外部打分器——
   我们的 `draft_validate` + 验收测试恰好就是现成的 evaluator。
   **对接点**：形态 C 的提案竞争可以从"首个通过者胜出"升级为 bandit 路由：
   边界拒绝/校验通过就是天然的奖励信号，几轮之后集群自动学会"schema 密集步骤派谁"。
2. **TRINITY**（arXiv:2512.04695，ICLR 2026）：0.6B 协调器 + 约 10K 参数选择头，
   用进化策略（sep-CMA-ES，非 RL）只学"派谁、什么角色"（Thinker/Worker/Verifier 三角色），
   技能全部在池中模型，LiveCodeBench SOTA。
   **对接点**：形态 B 的状态机是"协调逻辑归代码"的极端；TRINITY 给出中间形态——
   状态机管阶段推进，一个极小的学习型路由头管阶段内派谁。可作为形态 B 的 v2。
3. **Fugu 软标签路由**（arXiv:2606.21228）：用各 worker 的实测表现构造软分布做 KL-SFT，
   再用进化/GRPO 端到端细化。**对接点**：我们每轮 turn 记录 model + 工具结果 + 校验结局，
   构建历史就是现成的路由训练集——先跑形态 A/C 攒数据，路由器可以以后再学。
4. **生成与判定分离是全线共同前提**：AB-MCTS 靠外部打分器、TRINITY 有独立 Verifier 角色、
   AI Scientist 有校准过的自动审稿人、DGM 论文实测到 agent 伪造测试日志/删除检测标记。
   这与本平台"验收由确定性机制执法、交付验证独立完成"的纪律完全同构——
   论文里可以引 DGM 的 reward hacking 实证作为"为什么执法必须在模型外"的旁证。
5. **弱模型的价值边界**（CoffeeBench，arXiv:2606.16613）：弱模型在**无裁判的长时程自主**
   环境里会"思而不行"；在**有裁判的搜索**里有正贡献（AB-MCTS 中单独很弱的模型
   帮助组合解出无人能单独解出的题）。builder 的每一步都过硬门，恰好落在有利侧——
   这条边界本身就是论文 discussion 的好素材。

**定位空白（对论文最重要）**：Sakana 已证明"小协调器 + 前沿大模型池"（TRINITY/Fugu）
和"协调器与基座无关"（Gemma 4 E2B 版 Fugu），但**"小协调器 + 纯小模型 worker 池"
的组合尚无公开实证**（其技术报告明确不讨论小模型当 worker 的设定）。
我们的形态 B/C（状态机或极小路由 + 全小模型执行体 + 执法 harness）正好填这个空白，
且差异化主张不同：Sakana 靠学习型编排提升上限，我们靠边界执法保住下限。

## 7. 实验设计（论文视角）

| 臂 | 协调 | 执行 | 验证的命题 |
| --- | --- | --- | --- |
| 对照 | 大模型 | 大模型 | 现状基线 |
| A1 | 小模型(30B) | 配置手=小模型(4-7B) | harness 下弱模型能否交付 |
| A2 | 大模型 | 逐角色下放（仅配置手 / 加测试作者） | **下放原则验证**：配置手下放应近无损，测试作者下放应推高静默失败率 |
| B1 | 状态机 | 小模型(7-14B) | 控制流归代码后模型能力还剩多少影响 |
| B2 | 状态机 | 极小模型(≤4B)+升级阶梯 | 能力下限与成本曲线 |
| 消融叉乘 | 任一 | 任一 | 关防线后小模型臂的崩溃幅度应显著大于大模型臂 |

A2 是 §3 下放原则（可下放度 = 硬门覆盖度 × 调用频次）的直接检验：两个方向的
预测都明确、可证伪——故意把测试作者下放给小模型作为"反面臂"，预期静默失败率
显著上升，恰好反证硬门缺位处不可下放。

最后一行是论文最有力的预期结果：**如果防线消融对小模型臂的伤害远大于大模型臂，
就证明了 harness 承担的正是大模型用参数换来的那部分可靠性**——两条主张
（防线有效、harness 与模型无关）在同一组实验里互相印证。

主指标沿用消融文档：一次通过率、静默失败率、修复循环数、单构建成本、泄漏率；
新增 per-模型 token 占比与边界拒绝率（提案被 schema/revision 拒绝的比例——
衡量"他律"实际接住了多少错误）。

## 8. 落地顺序建议

0. ✅ **Builder 注册表**（2026-08-18 已落地）：`builder_registry.py` 提供
   `BuilderEngine` 协议 + `BuilderRegistry`；builds 表新增 `builder` 列（默认
   classic，老记录自动回落），`BuildRequest.builder` 按名选择引擎，API 全部
   入口（创建/取消/插话/续跑/返修/worker 认领）按构建记录路由。现有单模型
   实现注册为 `classic`；新 builder 实现（如 `small-ensemble`）在 api 装配处
   并排 register 即可参与对照实验。harness 暂不抽公共层，跟随各自 builder。
1. ✅ **per-actor model 改造**（2026-08-18 已落地）：`_agent_loop` 支持 per-actor
   `model`；`BuildRequest.coordinator_model` / `teammate_models`（存 team_state，
   进配置指纹）；`spawn_teammate` 工具带 `model` 字段（enum 白名单，越界硬门
   报错）；`TeammateState.model` 落库保证 send_message 追问不漂移；每轮 turn
   记录、usage 记账、transcripts 全部按 actor 模型记血缘。
2. 角色预设表（角色 → 模型 + 工具子集 + 系统提示片段）——配置化，不写死；
3. ✅（代码侧）**原生 chat-completions 接入**（2026-08-18 已落地）：不走 Anthropic
   兼容端点——协议抽象是平台自己的，`providers/openai_chat.py` 把 vLLM/SGLang/
   Ollama 的标准 chat-completions 流翻译成内部事件；`local/` 前缀由
   `LOCAL_MODEL_BASE_URL` 注册（见 .env.example）；回环地址豁免 egress 开关。
   剩余：起真实 vLLM 端点 + 下载模型做冒烟（等带宽方便时）；
4. 形态 B 状态机：作为新引擎注册（如 `mechanical`），复用全部 `_execute` 执法逻辑；
5. 形态 C 提案竞争 + 升级阶梯；
6. 与 harness-config-ablation.md 的 defenses 开关汇合，跑 §7 实验矩阵。
