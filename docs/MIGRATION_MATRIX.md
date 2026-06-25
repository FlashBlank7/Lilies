# TypeScript → Python 核心能力迁移矩阵

该矩阵以行为为迁移单位。原 Claude Code TypeScript 快照现在位于 `references/claude-code/src/`，当前平台实现位于 `platform/backend/src/agent_platform/`。原快照缺少构建配置，并存在大量缺失内部模块，因此 `ported` 表示关键行为已在 Python 中重建，而不是逐行翻译。

| TypeScript 能力 | Python 实现 | 状态 | 说明 |
|---|---|---|---|
| `query.ts` agent loop | `runtime.py` | ported | 多轮模型/工具循环、最大轮次、重试、取消、预算 |
| `QueryEngine.ts` 会话入口 | `runtime.py`, `api.py` | adapted | 后端 session + REST/SSE，取代 CLI/SDK 入口 |
| `services/api/claude.ts` | `providers/base.py`, `providers/deepseek.py` | adapted | 统一 Provider；当前用 DeepSeek Anthropic-compatible API |
| `Tool.ts` | `tools/base.py` | ported | 工具 Schema、注册、上下文与结果抽象 |
| Read/Write/Edit | `tools/core.py` | ported | Docker 内工作区限制、精确编辑 |
| Glob/Grep/Bash | `tools/core.py` | ported | ripgrep 和受资源限制 Shell |
| Task tools | `tools/core.py` | adapted | 工作区 `.agent/tasks.json` 持久化 |
| `AgentTool` | `runtime.py`, `tools/core.py` | ported | 独立消息、沙盒、权限、预算、事件流、深度和轮次限制 |
| `SkillTool` | `models.py`, `tools/core.py` | adapted | AgentSpec 内版本化 Skill |
| MCP tools | `tools/mcp.py` | adapted | HTTP/stdio JSON-RPC tools/call |
| permission hooks | `permissions.py`, `api.py` | ported | SSE 请求、allow/deny、输入更新、暂停恢复 |
| system prompt assembly | `prompts.py` | adapted | 动态身份、工作区、日期、工具、Skill、MCP |
| auto compact | `runtime.py` | ported | 阈值、摘要、近期消息保留、事件 |
| transcript/session storage | `storage.py` | adapted | SQLite WAL 状态 + JSONL 事件回放 |
| streaming events | `providers/deepseek.py`, `api.py` | ported | text/thinking/tool/SSE 与 Last-Event-ID |
| agent definitions | `models.py` | ported | AgentSpec、工具、Skill、MCP、权限、网络、验证 |
| agent generation wizard | `builder.py`, `workflow_runtime.py` | adapted | 团队通过增量积木工具搭建、测试、修复和发布，禁止整图输出 |
| TeamCreate / teammate mailbox | `builder.py`, `workflow_storage.py` | adapted | 动态队友、独立消息历史、mailbox 唤醒和持久团队状态 |
| TaskCreate/Get/List/Update | `builder.py` | ported | owner、blocked_by、acceptance 和状态流转 |
| coordinator mode | `builder.py` | adapted | 协调器提示/工具约束，不硬编码领域角色 |
| WorkflowTool / workflow orchestration | `blocks.py`, `workflow_runtime.py` | adapted | 16 类 Dify 风格积木、DAG、分支、Iteration、Loop、暂停恢复 |
| React/Ink terminal UI | — | backend-excluded | 由 REST/SSE 和轻量调试页替代 |
| Anthropic OAuth/private APIs | — | backend-excluded | 后端通过 Provider 环境密钥调用 |
| analytics/feature flags | — | backend-excluded | 不影响智能体执行质量 |
| voice/IDE bridge/terminal themes | — | backend-excluded | 非后端核心 |
| LSP/Notebook/Worktree | — | pending | 可作为后续工具插件迁移 |

## 已知差异

- DeepSeek 不完全支持原 Anthropic API 的缓存、图片和部分 MCP content block；Provider capability 层用于显式降级。
- DeepSeek thinking 模式拒绝强制 `tool_choice`；Agent Factory 使用 auto tool choice，并通过生成提示和两次结构修复强制得到 AgentSpec。
- 当前 MCP stdio 实现针对 line-oriented JSON-RPC；需要 Content-Length framing 的旧服务器需增加 transport adapter。
- `allowlist` 网络策略尚未形成强制 egress 防火墙；`full` 和 `none` 已由 Docker 网络层执行。
- 原快照中的 CLI 命令、展示逻辑和私有服务不会进入 Python 后端。

## 真实验收记录

- 2026-06-23：使用真实 DeepSeek V4 Flash、Docker 沙盒和失败的 Python 测试项目完成闭环。
- 生成阶段创建 AgentSpec，在 Docker 中把错误的加法实现修复并执行 `python -m pytest -q`，结果为 `2 passed`。
- 发布后创建新会话，完成 thinking 流、并行 Bash、两项权限批准、Read 工具和最终响应；宿主机复验仍为 `2 passed`。
