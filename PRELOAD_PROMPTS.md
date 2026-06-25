# 预加载 Prompts

这些 prompts 用于后续继续重构 Dify、Claude Code 和本平台时预加载。目标是让新的工作回合一开始就继承当前经验：目录边界、启动方式、调试方法、手动编辑器状态同步和测试门禁。

## 1. 项目上下文 Prompt

你正在维护一个“将脱胎于Claude Code Agent框架作为大脑 + Dify 风格积木”的智能体工作流平台。

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
2. 智能体团队通过受控积木 API 增量搭建 Workflow。
3. 产物必须可视化、可编辑、可测试、可发布。
4. Claude Agent 节点复用完整 AgentRuntime；不要把复杂能力藏成一次性代码生成。
5. 发布版本不可变；Draft 可继续人工微调。

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

参考 Dify 时只学习产品和积木编排思想，不复制其前端代码或品牌。

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
