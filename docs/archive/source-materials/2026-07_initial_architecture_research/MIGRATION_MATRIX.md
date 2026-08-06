# 核心能力实现矩阵

本文档记录 Lilies 平台的核心能力及其实现位置。所有能力均为原创 Python 实现，基于 Agent 系统设计的通用模式和 Anthropic Messages API 公开协议。积木系统的设计参考了 MIT 许可的 Dify 开源项目（`references/dify/`）的节点分类理念。

## 设计理念

Lilies 将 Agent 运行时能力分解为可独立测试、可替换、可组合的积木块。这种分解方式的核心洞察是：Agent 系统由 Harness（确定性的执行框架）和 LLM（非确定性的语义推理）复合而成。Lilies 的贡献是将两者在 DAG 拓扑层面显式组合，而非隐藏在单一节点内部。

| 能力 | 实现文件 | 说明 |
|------|---------|------|
| 多轮 Agent 循环 | `runtime.py` | 模型调用→工具执行→结果回灌→循环，支持最大轮次、重试、取消、预算 |
| 会话管理 | `runtime.py`, `api.py` | REST session + SSE 事件流 |
| LLM Provider 抽象 | `providers/base.py`, `providers/deepseek.py`, `providers/multi.py` | 统一 Provider 接口；当前支持 DeepSeek Anthropic-compatible API；可扩展到多 Provider |
| 工具系统 | `tools/base.py` | 工具 Schema、注册表、执行上下文、结果抽象 |
| 文件操作工具 | `tools/core.py` | Read、Write、Edit — Docker 沙盒内受限制的文件操作 |
| 搜索与执行工具 | `tools/core.py` | Glob (ripgrep)、Grep、Bash — 受资源限制的执行 |
| 任务管理 | `tools/core.py` | 工作区持久化任务列表 |
| 子 Agent 工具 | `runtime.py`, `tools/core.py` | 独立上下文、沙盒、权限、预算的子 Agent 机制 |
| Skill 工具 | `models.py`, `tools/core.py` | AgentSpec 内版本化 Skill 指令加载 |
| MCP 工具 | `tools/mcp.py` | HTTP/stdio JSON-RPC 协议支持 |
| 权限系统 | `permissions.py`, `api.py` | SSE 权限请求、allow/deny、3 级模式 (always_ask/plan_first/auto_approve) |
| System Prompt 组装 | `prompts.py` | 动态身份、工作区上下文、日期、工具、Skill、MCP |
| 上下文压缩 | `runtime.py` | 阈值触发、摘要生成、关键事实保留、事件通知 |
| 会话存储 | `storage.py` | SQLite WAL 状态管理 + JSONL 事件流 |
| 流事件处理 | `providers/deepseek.py`, `api.py` | 基于 Anthropic Messages API 公开协议的 text/thinking/tool 事件 + SSE |
| Agent 定义 | `models.py` | AgentSpec — 工具、Skill、MCP、权限、网络策略、验证 |
| Builder Team | `builder.py`, `workflow_runtime.py` | Agent 团队通过积木工具增量搭建、测试、修复、发布 |
| 多 Agent 协作 | `builder.py`, `workflow_storage.py` | 动态子 Agent、独立消息历史、mailbox 唤醒 |
| 任务编排 | `builder.py` | 依赖关系、owner、acceptance criteria、状态流转 |
| 协调器模式 | `builder.py` | 协调器 Prompt + 工具约束，动态角色 |
| 工作流编排 | `blocks.py`, `workflow_runtime.py` | 16 类业务积木 + 25 类 Agent 架构积木、DAG 执行、分支、迭代、循环、暂停恢复 |
| 钩子系统 | `workflow_runtime.py` (hook_point) | SSE 事件钩子，供外部系统监听 |
| 模板系统 | `template_store.py`, `template_models.py` | Fork 模型、置信度追踪、自动推荐 |
| 元认知层 | `meta_cognition.py`, `extraction_gate.py`, `merge_engine.py` | 决策追踪→提取→门控→合并→推荐 |
| 可观测性 | `observability.py` | 运行指标、成本归因、失败聚类 |
| 编排建议 | `orchestration_advisor.py` | 需求关键词→积木组合推荐 |

## 不包含的能力

以下能力属于特定产品的 CLI/IDE 功能，不属于 Lilies 平台层：
- 终端 UI / 交互式 CLI
- OAuth 认证流程
- 使用分析 / feature flags
- 语音 / IDE bridge
- LSP / Notebook 集成

## 已知技术约束

- DeepSeek Anthropic-compatible API 不完全支持原 Anthropic API 的缓存、图片和部分 MCP content block；Provider capability 层用于显式降级
- DeepSeek thinking 模式在某些场景下与 `tool_choice` 存在交互限制；Agent Factory 通过 auto tool choice + 结构修复实现兼容
- MCP stdio 当前针对 line-oriented JSON-RPC；Content-Length framing 需增加 transport adapter
- 网络策略 `allowlist` 尚未形成强制 egress 防火墙；`full` / `none` 由 Docker 网络层执行

## 真实验收记录

- 2026-06-23：使用真实 DeepSeek V4 Flash、Docker 沙盒完成代码修复闭环（Read → Run Tests → Debug → Edit → Retest, 2/2 passed）
- 2026-06-25：Builder Team 成功搭建发表工作流（4 节点，108s），Agent Factory 生成 Python Code Reviewer（6 工具, 5028 字符 prompt）
- 2026-06-26：多 Agent 代码生成 — Coder + Tester Agent 协作产出 536 行代码 + 76 个测试, 76/76 passed
- 2026-06-29：元认知层全链路验证 — DecisionTracker → ExtractionGate → MergeEngine → TemplateStore, 12/12 测试通过
