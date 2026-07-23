# 预加载 Prompts

这些 prompts 用于后续继续建设智能体/工作流生成平台时预加载。目标是让新的工作回合一开始就同时继承产品意图和工程经验，避免只记住 Dify、Claude Code、目录、调试和测试机制，却遗失目标客户与真实企业问题。

## 1. 项目上下文 Prompt

你正在维护一个“以 Claude 式智能体架构支持莉莉丝 + Dify 风格积木与运行产品”的智能体/工作流生成平台。

名称必须区分：

- **智能体/工作流生成平台**：完整产品。
- **莉莉丝**：平台内部负责询问、规划、搭建、测试和修复工作流的具名智能体。
- **平台生成的工作流**：交付给客户并接受真实业务验收的产物。

产品最高意图见 `docs/PRODUCT_NORTH_STAR.md`。平台面向尚未完成 AI 化、但有明确业务或技术流程需要自动化和智能化的传统企业。Claude/Codex 架构是智能底座，Dify 是产品范式参考，二者都不是目标客户或默认场景。

当前平台实现位于：

```text
platform/backend/src/agent_platform/
platform/frontend/
```

外部上游项目只作为参考，统一放在：

```text
references/<project-name>/
```

不要把外部项目源码放回根目录 `src/` 或 `web/`。看到 `platform/` 才是当前要交付的软件；看到 `references/` 只是迁移参考。

核心产品目标：

1. 用户用自然语言描述需求。
2. 莉莉丝通过选项式询问识别客户角色、数据、已有模型、业务规则、外部系统、交付物和验收缺口。
3. 莉莉丝通过受控积木、模块与平台服务增量搭建 Workflow。
4. 平台能够逐步表达并运行企业 ML/DL、RAG、结构化数据、Excel/文件交付、API 写回和人机复核等真实工作流。
5. 产物必须可视化、可编辑、可测试、可发布，并接受独立业务结果验收。
6. Claude Agent 节点复用完整 AgentRuntime；不要把复杂能力藏成一次性代码生成。
7. 发布版本不可变；Draft 可继续人工微调。
8. 经过真实项目验证的工作流和模块可以沉淀为行业知识资产。

场景选择前必须回答：目标传统企业角色是谁、真实问题是什么、输入数据/模型/系统/人工角色/最终交付物是什么、平台生成的工作流完成什么、独立业务 oracle 是什么。Codex、GitHub、爬虫、Connector 或治理任务不满足这些问题时，只能作为基础设施压力测试，不能进入平台客户成功率分母。

## 2. 启动与环境 Prompt

优先使用一键脚本：

```bash
./scripts/dev_platform.sh --check-env
./scripts/dev_platform.sh
```

正常环境检查输出：

```text
DEEPSEEK_API_KEY ok
API_TOKEN ok
```

启用 `LILIES_LOCAL_AGENT_ENABLED=true` 时，脚本还会显示 Local Lilies
assignment 的实际回调地址。未显式配置 `LILIES_PLATFORM_BASE_URL` 时，
它必须与本次 API host/port 一致，默认是 `http://127.0.0.1:8001`，不能
回落到应用配置中的 `8000`。

本地开发端口：

- API：`http://127.0.0.1:8001`
- Studio：`http://127.0.0.1:3000`

如果前后端分两个终端手动启动，第二个终端也必须执行：

```bash
set -a; source .env; set +a
```

前端开发模式默认使用 webpack dev server，避免 Next 16/Turbopack 本地 panic。Turbopack 只用：

```bash
cd platform/frontend
npm run dev:turbo
```

每次改启动方式后，必须同步更新：

1. `README.md`
2. 本文件的“启动与环境 Prompt”
3. 相关 debug/test 文档

## 3. Debug 方法论 Prompt

遇到运行问题时，不要只猜。按顺序取证：

1. 先分层：是后端 API、前端代理、Next dev server、浏览器状态、Docker sandbox，还是模型 Provider。
2. 看真实日志：API status、Next 输出、panic log、浏览器网络请求。
3. 用最短命令验证环境：

```bash
./scripts/dev_platform.sh --check-env
curl -sS http://127.0.0.1:8001/health
cd platform/frontend && npm run lint
```

4. 如果是 token 问题，确认 `.env` 中 `DEEPSEEK_API_KEY` 和 `API_TOKEN` 均存在；前端页面也可输入本地 API_TOKEN。
5. 如果是端口问题，找出旧进程后再重启，不要让两个 dev server 同时跑。
6. 如果是 Next/Turbopack panic，默认切回 webpack dev server。
7. 前端 Studio 要避免依赖新浏览器 API 造成黑屏，例如优先用 reduce 替代 `Object.groupBy`。
8. 修复后必须把复现、根因、验证命令写回 README 或 docs。

## 4. 手动 Workflow Editor Prompt

手动编辑器的事实来源是后端 Draft。React Flow 只是展示和交互层。

必须保持这些不变量：

1. Delete 键、本地删除、按钮删除都要写回后端 Draft。
2. 删除节点必须清理相关边。
3. 删除当前选中节点后，必须清空 `selected` 和配置编辑器。
4. 连接线也必须有明确选中态，但点击线只能高亮画布，不应强制切换左侧 tab 或覆盖当前检查器内容；Delete/Backspace 要可删除选中的线。
5. 拖线连接 Variable Aggregator 时，必须同步写入 `config.variables` 的 `$ref`。
6. 删除画布边后，必须同步清理目标节点配置中指向源节点的 `$ref`；删除 Aggregator 配置中的 `$ref` 后，也必须同步删除对应画布边。
7. 保存配置后必须刷新 Draft，并让选中节点配置显示最新快照。
8. 每次 Draft 写操作必须使用 `expected_revision` 和 `idempotency_key`。
9. 人工修改 Draft 后测试状态必须失效，不能发布未重新验证的内容。

已知重点回归：

- 新建节点 → 选中 → Delete → 再新建节点，旧节点不能复活。
- 点击连接线 → 只高亮连接线，不切换左侧面板；按 Delete/Backspace 删除后刷新不能恢复。
- 新建连接后立即删除，必须按 Draft 中真实边 id 或 source/target 解析后端边，避免本地边 id 导致 404。
- 删除连接线后，目标节点配置不能残留指向源节点的 `$ref`。
- 拖线连接新变量聚合器后，配置面板必须立即出现 `$ref`。
- 删除配置中的连接 `$ref` 后，画布边必须消失。

详见：

```text
docs/MANUAL_EDITOR_TEST_PLAN.md
```

## 5. Dify 重构 Prompt

参考 Dify 时学习产品和积木编排思想，不复制其前端代码或品牌。平台对标的是可视化 AI 工作流产品范式，并用莉莉丝增加自动工作流生成和修复能力。

要重点观察：

1. 节点目录如何分组。
2. 节点配置 schema 如何约束。
3. 变量引用和端口类型如何表达。
4. 分支、循环、迭代、Human Input 如何落到可视化图。
5. Draft 和发布版本如何隔离。
6. 测试、运行 trace、节点输出如何帮助人工微调。

迁移到本平台时必须保持：

- AI 团队只能通过积木 API 增量搭建。
- 不允许 AI 直接输出整张 Graph JSON 作为最终产物。
- 不开放任意 Code 节点绕过积木系统。
- 每个写操作带 revision 和 idempotency。
- 企业数据、ML/DL、RAG、文件工件和客户 API 能力不能被通用 LLM 文本节点冒充。

## 6. Claude Code 重构 Prompt

参考 Claude Code 时，要保护“智能体智能”：

1. 多轮 agent loop。
2. 工具调用和工具结果。
3. 权限请求与恢复。
4. 上下文注入和自动压缩。
5. Skill/MCP。
6. 子智能体、mailbox、任务依赖、持久队友。
7. 独立上下文、预算、取消和工作目录隔离。

不要把 Claude Agent 简化成单次 LLM 调用。LLM 节点可以是单次调用，但 Claude Agent 节点必须复用完整 AgentRuntime。

Claude/Codex 类任务用于验证莉莉丝的智能体架构时必须单独报告。即使莉莉丝完成真实代码任务，也不能据此声明平台生成的工作流已经解决传统企业 AI 化问题。

## 7. 验证 Prompt

修改后至少运行：

```bash
.venv/bin/ruff check platform/backend/src/agent_platform tests scripts
.venv/bin/pytest -q
cd platform/frontend && npm run lint && npm run build
```

如果改了启动：

```bash
./scripts/dev_platform.sh --check-env
```

启用 Local Lilies 时还要确认上述输出中的 callback 与实际 API 端口一致。

如果改了 Docker：

```bash
docker compose config --quiet
```

如果改了手动编辑器，至少手工验证：

1. Delete 删除节点不复活。
2. Aggregator 连线同步配置。
3. Aggregator 配置删除 `$ref` 同步删除边。
4. 刷新页面后状态不回退。

验证结果要写入最终回复；如果没有跑某项，明确说明原因。
