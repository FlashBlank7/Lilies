# asset_clyins_workflow_as_product

## 1. 核心结论

Clyins 证明了 Lilies 方法论的**产品化可行性**：一个完整的 AI 产品（AI 项目经理）可以仅由现有积木搭建的 BlockFlow + 一个薄 Web 界面构成。核心理念——"调 harness 不调 agent"——在 Clyins 身上得到了**自我指涉式验证**：

> Clyins 本身就是一个 Harness + LLM 复合体 BlockFlow。它用 Lilies 搭建，生成的结果可以投喂给 Lilies 执行。

由此得到三个稳定推论：

1. **BlockFlow 不仅是开发工具，也是产品交付形态**。当架构无法在积木层面继续革新时，把它打包成服务（Web 界面 + API）就是产品化的正确路径。
2. **"模板 → 使用 → 进化"飞轮在 Clyins 上同样适用**。Builder Team 可以展开 Clyins 模板，根据具体场景定制（如添加钉钉通知、日历集成），验证通过后进化回模板市场。
3. **平台自我引用是成熟度的标志**。继 Evolution Pipeline 之后，Clyins 是 Lilies 上第二个"用平台自身搭建平台能力"的案例。

## 2. 获得成本

这个资产来自从概念分析到完整实现的六轮迭代：

- 第 1 轮：阅读全部源码，理解 Lilies 架构
- 第 2 轮：分析 Clyins 概念对话（talk1~5.txt），产出设计分析报告
- 第 3 轮：编写 `templates/clyins.json` BlockFlow 模板 + 测试
- 第 4 轮：端到端运行，发现并修复 task_dispatcher title 回退和 schedule_table 渲染问题
- 第 5 轮：搭建 Web 界面（run-view.html）+ API 端点（POST /api/v1/clyins/run）+ natapp 公网部署
- 第 6 轮：添加登录保护 + 文档整理

## 3. 证据链

**概念来源**:
- `docs/source-materials/ideas/talk1.txt` ~ `talk5.txt` — Clyins 概念从模糊到清晰的完整对话记录

**设计文档**:
- `docs/current-design/design_clyins_blockflow.md` — BlockFlow 架构细节
- `docs/stage-reports/V1.4_clyins_project_manager.md` — 阶段报告

**代码锚点**:
- `templates/clyins.json` — Clyins BlockFlow 模板（7 节点 6 边）
- `platform/backend/src/agent_platform/api.py` — POST /api/v1/clyins/run 端点 + auto-resume
- `platform/backend/src/agent_platform/workflow_runtime.py` — task_dispatcher title 回退
- `mobile_app/run-view.html` — Web 上传/查看界面（含登录保护）
- `tests/test_workflow.py` — 3 个 Clyins 专项测试
- `demo_clyins.py` — 端到端演示脚本

**理论支撑**:
- `docs/intellectual-assets/asset_harness_llm_composite.md` — Harness + LLM = 不可再分的结构不变量
- `docs/intellectual-assets/asset_blockflow_language_system.md` — BlockFlow 术语体系

## 4. 适用边界

适用于：

- 判断一个业务需求是否应该被实现为 BlockFlow 而非独立系统
- 设计 AI 产品的"薄界面 + BlockFlow 核心"架构模式
- 理解平台自我引用（Platform Self-Reference）在 Lilies 中的实现方式
- 评估 Web 上传 → BlockFlow 执行 → 结果展示这一交互模式的可行性

不适用于：

- 声称所有复杂系统都应该用单个 BlockFlow 实现
- 忽略 Clyins 当前的限制（无持久化项目状态、无语音输入、无日历集成）
- 把 Clyins 的 Web 界面模式当作 Lilies 前端开发的唯一范式

## 5. 复用方式

评估新业务需求时，使用 Clyins 作为参考模版：

| 问题 | Clyins 的做法 |
|------|-------------|
| 业务逻辑放哪里？ | BlockFlow 模板（templates/clyins.json） |
| 用户交互怎么做？ | 薄 HTML 页面 + REST API（mobile_app/run-view.html） |
| LLM 输出如何结构化？ | structured_output + 预格式化文本字段（schedule_table） |
| 人工确认环节？ | human_input 积木 + Web 端自动恢复 |
| 如何部署？ | natapp 隧道 → uvicorn（零侵入现有服务） |
| 如何保护？ | 前端登录密码 + API Bearer Token |

## 6. 禁止滥用场景

- 不要因为 Clyins 成功了就认为"所有产品都应该是一个 BlockFlow + 一个 HTML"
- 不要让 Clyins 的 Web 界面模式绕过 Platform Harness 的 task monitor 治理
- 不要把 Clyins 的前端登录（客户端密码校验）当作生产级安全方案——它适用于内部工具场景，对外部署需要服务端认证
- 不要忽略 Clyins 的持久化限制——当前每次生成是独立的 WorkflowRun，跨会议跟踪需要 Project 模型支持
