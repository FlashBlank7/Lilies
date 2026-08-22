# DeepSeek 官方 Agent Harness 调研报告（截至 2026-08-18）

> 调研日期：2026-08-18。本文为调研代理产出的原始报告归档，供论文引用与复核。
> 官方来源：github.com/deepseek-ai、deepseek.com/harness、api-docs.deepseek.com、
> arXiv 官方论文、官方 X 帐号；第三方来源已逐条标注。

## 一、结论先行

**"DeepSeek 有自己的 harness" 不是误传**：DeepSeek 于 **2026-08-13** 正式开源了名为
**DeepSeek Harness（dsh）** 的官方 agent harness（MIT 协议），5 天内 GitHub star 达
154,026（GitHub API 直接核实，仓库创建于 2026-08-13T11:56Z，归属真实 deepseek-ai org）。
定位为 Claude Code 的开源对标物，同时是 DeepSeek 官方跑 agentic 基准的脚手架。
在它之前，官方脚手架以碎片形式存在（V3.1 的 agent 轨迹模板、V3.2 论文的内部评测框架），
dsh 是这条线的集大成产物。

## 二、确凿存在的官方 harness / 框架

### 1. DeepSeek Harness（dsh）——官方，开源，主角

- **是什么**：官方 agent harness / 运行时，口号 "Everything is a Plugin"。模型适配器、工具、技能（skills）、会话、沙箱、存储、agent 循环、调度、UI 全部是可热插拔插件。
- **发布**：2026-08-13 官方 X 宣布 v0.1 Developer Preview，MIT 开源。npm 包 `@deepseek-ai/dsh` 当前 0.1.0-rc.7（2026-08-17）——仍在 rc 阶段，官方明确警告会有破坏性变更。
- **内核**：构建在 **Cordis** 插件框架之上（注意：Cordis 非 DeepSeek 原创，是开发者 shigma 的既有开源项目、Koishi 框架内核）。核心机制：`ctx.effect` 可逆副作用（类 RAII/Drop）、生命周期事件、服务依赖图——插件卸载时副作用可完全回滚，安装/卸载/换 UI 无需重启。（来源：floatboat.ai 第三方技术分析）
- **四种预设模式**（官方文档 <https://deepseek.com/harness/en/>）：
  - **Standard**：完整编码 agent（文件编辑、shell、搜索、skills、计划）；
  - **Code（PTC）**：工具经 TypeScript SDK 暴露，模型写程序把多步工具调用合并为单次执行；
  - **Minimal**：只有持久 bash + 字符串替换编辑器——**官方跑基准就用这个模式**；
  - **Creator**：自建 preset / 插件实验 / 运行时检查。
- **其他**：append-only 会话日志（可重放、搜索、fork）；插件发现靠 GitHub topic `dsh-plugin`（发布不足 24 小时约 288 个插件仓库，36kr 报道）。
- **链接**：<https://github.com/deepseek-ai/deepseek-harness> | <https://deepseek.com/harness/en/> | `npx @deepseek-ai/dsh web`
- **媒体佐证**：The New Stack（2026-08-13）、VentureBeat。

### 2. dsh 之前的官方脚手架谱系

- **V3.1 / V3.1-Terminus agent 轨迹模板**（2025-08/09）：HF 模型卡附 `code_agent_trajectory.html` 等，定义 code/search agent 官方工具调用轨迹格式——"模板级"脚手架。
- **V3.2 论文内部评测框架**（2025-12，arXiv:2512.02556，未开源）：SWE-bench Verified 主分数用 "our internal framework"，在 Claude Code、RooCode 下交叉验证（72-74 一致）；Terminal-Bench 2.0 的 46.4 用 Claude Code 框架跑。RL 训练侧合成 1,827 个通用 agent 环境、24,667 个 code 环境、50,275 个 search 任务。
- **衔接**：V4-Flash（2026-07-31）agent 基准成绩标注使用 "DeepSeek Harness 极简模式"（第三方转述）。可合理推断 dsh Minimal 是内部框架的开源化，但**官方未明确确认**。

## 三、DeepSeek 模型 agentic 使用的官方推荐方式（API 层现状）

**模型现状**：API 提供 `deepseek-v4-pro`（V4-Pro-0813）和 `deepseek-v4-flash`（V4-Flash-0731）。V4 系列 2026-04-24 发布（V4-Pro 1.6T 总参/49B 激活，1M 上下文）。**R2 至今未发布**。

1. **thinking 模式与工具调用多轮协议**（<https://api-docs.deepseek.com/guides/thinking_mode>，最重要）：
   - thinking 默认开启（默认 effort=high）；自 V3.2 起 thinking 下支持工具调用（思考与工具调用交织）。
   - **工具调用链中，中间 assistant 消息的 `reasoning_content` 必须原样回传 API，否则 400**。仅在新 user 消息出现时才丢弃历史思考内容。
   - thinking 模式不支持 temperature/top_p/presence_penalty/frequency_penalty。
2. **Strict 模式（Beta）**（<https://api-docs.deepseek.com/guides/tool_calls>）：走 `api.deepseek.com/beta` + `strict: true`。硬性要求所有属性 required、`additionalProperties: false`；不支持 minLength/pattern/format/minimum 等。
   - **已知 bug**：曾返回畸形 JSON（首属性键缺闭合引号），官方 issue #1069（2025-12）；2026-07 第三方实测 34 项 strict 断言只过 14 项（chat-deep.ai，社区数据）。**结论：strict 模式不可作为唯一防线，出参校验必须自己做。**
3. **JSON mode**：`response_format: {type: 'json_object'}`，prompt 必须含 "json" 并给示例；官方承认偶发返回空 content；无 json_schema 级结构化输出。
4. **双协议兼容**：同时支持 OpenAI 格式（`thinking: {type}`）与 Anthropic 格式（`reasoning: {effort}`）；官方给出 Claude Code、GitHub Copilot、OpenCode 接入指引。
5. **运营层重大变化**：2026-08-16 起 API 改**峰谷计费**，涨幅 50%~1100%。V4-Pro 输出 token 从 $0.87/M 涨到峰时 $3.96/M、谷时 $1.98/M；cache-miss 输入峰时 $1.32/M（Reuters/Engadget/InfoWorld，2026-08-13 公告）。

## 四、对自研 harness 的可借鉴点

1. **Minimal 模式哲学与 Lilies 同构**：官方基准配置只有"持久 bash + 字符串替换编辑器"——工具面越小，模型行为越可控、越可测。可对照 dsh Minimal 工具 schema 校准我们的工具边界。
2. **Cordis 可逆副作用（`ctx.effect`）值得抄**：插件/工具挂载时登记副作用、卸载时保证回滚——执法层可借鉴"效应登记 + 可回滚"做工具级 undo/隔离。
3. **append-only 会话日志 + 重放/fork**：与增量操作理念契合，dsh 做成了一等公民。
4. **合规红线**：thinking 模式工具循环必须原样回传 `reasoning_content`（否则 400）；strict 当辅助不当保险；JSON mode 需兜底空 content 重试。
5. **预算守卫升级为峰谷感知**：峰谷价差 2 倍，谷时跑批量 agent 任务直接省一半输出成本。
6. **PTC/Code 模式思路**：模型写 TS 程序合并多步工具调用为单次执行，省多轮 token。

## 五、未找到 / 不确定的信息

- dsh 对第三方模型供应商的官方支持范围（"provider-agnostic" 为第三方说法）。
- dsh 是否就是 V3.2 论文的 "internal framework"：官方未确认，属合理推断。
- 《A Programming Paradigm for Spatiotemporal Composability》论文发表处未独立核实。
- V4 技术报告中 harness 相关附录未逐页核查。
- 并行工具调用支持情况官方文档未明确。
- "首日 13k star" 来自 The New Stack；154,026 为 GitHub API 实时核实数。
