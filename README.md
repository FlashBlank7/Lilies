# Lilies — 智能体工作流平台

> **以搭建工作流的方式解决问题，而非以提升单个模型能力的方式。**

Lilies 是一个面向 AI 转型的工作流工程化平台。它的核心洞察是：

> *人和人工作能力的差距，往往不是智力或经验的差距，而是工作流的差距。好流程让普通人产出好结果，坏流程让聪明人也寸步难行。*

Lilies 把专家的做事方式变成**可执行、可复用、可迭代、可组合**的积木工作流。换一个更好的 LLM 能提升 20% 的质量，而固化一个好的工作流能提升整个团队的基线。两者叠加，就是 AI 转型的正解。

---

## 架构哲学

```
传统 AI 平台:  问题 → [更好的LLM/Agent] → 答案
                      ↑
                  关注"大脑"

Lilies:        问题 → [工作流模板] → [LLM as tool] → 答案
                      ↑                  ↑
                  关注"流程"          LLM 是可替换的执行器
```

### 三个核心设计原则

**1. 工作流是模块，模块是工作流**
任何已发布的工作流都可以作为积木被其他工作流调用。通过 `$ref` 引用机制和 Version 锁定，工作流之间实现零耦合组合。这使系统具有分形能力——一个复杂工作流可以由若干子工作流构成，而每个子工作流本身就是可独立运行的完整单元。

**2. 模块间通过结构化文本通信 — "文本即智能"**
模块的输出不是黑箱 JSON，而是 LLM 天然理解的结构化文本。下游模块可以读取上游的自然语言结果继续推理。这降低了集成成本：不需要为每个模块定义精确的 Schema，语义靠 LLM 自己理解，结构靠 `$ref` 精确引用。

**3. 固化工作流 > 提升 Agent 能力**
平台的真正价值不是造一个更好的大脑，而是造一个能固化大脑工作方式的骨架。6 个内置模板（代码审查、客服路由、数据分析、文档摘要、任务分解、长文生成）就是把常见的专家工作模式编码为可复用的积木组合。

---

## 已实现的核心能力

### 积木系统 — 41 个积木

| 类别 | 数量 | 示例 |
|------|------|------|
| 业务工作流积木 | 16 | LLM, If/Else, Question Classifier, Iteration, Loop, Human Input, HTTP Request, Template Transform, Variable Aggregator... |
| Agent 架构积木 | 25 | Context Assembler, Model Turn, Tool Call Router, Tool Executor, Permission Gate, Subagent Spawn, Task Dispatcher, Budget Gate, Hook Point, Checkpoint/Resume... |

每个 Agent 架构积木对应 Claude Code 源码中的一个具体运行时机制——从 Harness 中拆出来，变成可替换的积木。

### 模板市场 — 6 个内置模板

| 模板 | 用途 | 积木组合 |
|------|------|---------|
| code_reviewer | 代码审查与修复 | start → llm → end |
| customer_support_router | 智能客服路由 | start → classifier → if/else → 4×template → aggregator → end |
| data_analyzer | 数据分析流水线 | start → llm → parameter_extractor → template → end |
| document_summarizer | 文档摘要生成 | start → llm → template → end |
| task_decomposer | 任务分解与规划 | start → llm → template → end |
| long_form_writer | 万字长文生成 | start → llm → end (大纲)，可嵌套 Iteration 分章节 |

模板支持一键展开为可编辑的工作流草稿，也可以从草稿发布回模板库。

### Builder Team — AI 自动搭建工作流

输入自然语言需求 → Builder Team（AI 协调者 + 动态队友）自动：
1. 分析需求，拆解为任务
2. 搜索积木目录，阅读使用手册
3. 逐个添加节点、连线（不允许直接输出整图 JSON）
4. 生成验收测试
5. 运行测试，失败则修复后重试
6. 全部通过后发布

### Agent Factory — 自动生成专项 Agent

输入自然语言需求 → DeepSeek V4 Pro 自动生成包含 system_prompt、工具集、权限配置、预算限制的完整 AgentSpec → Docker 沙盒验证 → 发布。已验证可生成代码审查、数据分析、系统设计、测试工程等多种专项 Agent。

### 多 Agent 团队协作

通过 Subagent Spawn + Task Dispatcher + Dependency Gate + Mailbox 积木组合，可以编排多个专项 Agent 按依赖顺序协作。已验证的场景：需求拆解 Agent → 系统设计 Agent → 测试方案 Agent → 代码编写 Agent → 测试执行 Agent。

### Harness 级安全与治理

| 能力 | 实现 |
|------|------|
| 权限门 | 3 级模式：auto_approve / plan_first / always_ask |
| 预算控制 | Budget Gate — 基于 token 成本的硬限制 |
| 轮次限制 | Round Limit — 最大迭代次数硬限制 |
| 沙盒隔离 | Docker 容器，非特权，CPU/内存/PID 限制 |
| 钩子系统 | Hook Point — 外部系统可通过 SSE 监听工作流事件 |
| 检查点/恢复 | Checkpoint/Resume — 持久化到 SQLite，支持崩溃恢复 |
| 事件追踪 | Event Recorder — 结构化 SSE 事件流，完整审计轨迹 |

### 并发安全与确定性

- Revision 乐观锁：并发编辑草稿时，旧 revision 被 409 拒绝
- 幂等键保护：相同 idempotency_key 的重复操作被安全拒绝
- 运行隔离：已验证 5/10 并发运行零交叉污染
- 确定性保证：非 LLM 工作流 5 次同输入运行输出完全一致
- LLM 非确定性隔离：`structural_only` 模式只检查结构属性（exists/type/length），不检查内容相等

---

## 快速开始

```bash
# 1. 配置
cp .env.example .env
# 编辑 .env，设置 DEEPSEEK_API_KEY 和 API_TOKEN

# 2. 构建沙盒（使用宿主机 UID 避免权限问题）
docker build --build-arg SANDBOX_UID=$(id -u) --build-arg SANDBOX_GID=$(id -g) \
  -t agent-platform-sandbox:latest -f Dockerfile.sandbox .

# 3. 安装依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 4. 启动
./scripts/dev_platform.sh

# API: http://127.0.0.1:8001
# 调试页: http://127.0.0.1:8001/debug
# OpenAPI: http://127.0.0.1:8001/docs
```

---

## 目录结构

```
Lilies/
├── platform/
│   ├── backend/src/agent_platform/
│   │   ├── api.py                FastAPI、SSE、模板 API
│   │   ├── blocks.py             41 积木定义、Schema、端口、图校验
│   │   ├── builder.py            Builder Team（协调者+队友+任务+mailbox）
│   │   ├── factory.py            Agent 自动生成（需求→AgentSpec→验证→发布）
│   │   ├── runtime.py            Agent 多轮循环、工具执行、子Agent、权限
│   │   ├── workflow_runtime.py   DAG 拓扑执行、25 架构积木逻辑、checkpoint
│   │   ├── workflow_storage.py   草稿/版本/Build/Run 持久化、乐观锁
│   │   ├── template_store.py     模板市场（加载/搜索/展开/注册）
│   │   ├── template_models.py    模板数据模型
│   │   ├── providers/            ModelProvider 抽象 + DeepSeek 适配器
│   │   ├── tools/                核心工具（Read/Write/Bash/WebSearch...）
│   │   ├── sandbox.py            Docker 会话管理
│   │   └── storage.py            SQLite WAL + JSONL 事件
│   └── frontend/                 Next.js + React Flow Studio
├── templates/                    6 个内置工作流模板 (JSON)
├── tests/                        28 个单元/集成测试
├── docs/                         设计文档、迁移矩阵
├── examples/                     测试用示例项目
├── Dockerfile.sandbox            Agent 沙盒镜像
└── compose.yaml                  Docker Compose 编排
```

---

## 已验证的能力矩阵

| 维度 | 测试覆盖 | 结果 |
|------|---------|------|
| 单元测试 | 28 项 | 100% |
| 结构能力评估 | 49 项 (积木链/DAG/错误/并发/确定性) | 100% |
| 专家级测试 | 35 项 (嵌套/故障注入/权限矩阵/组合爆发) | 100% |
| 生产级增强 | 18 项 (非确定性隔离/并发/checkpoint) | 100% |
| 多 Agent 团队 | Agent 生成 100% + Agent 链执行 | 验证通过 |
| 产物工程可用 | 代码质量 + pytest + 冒烟测试 | 91.7% |

---

## API 速览

### 模板
```bash
GET  /api/v1/templates                     # 模板列表
GET  /api/v1/templates/{name}              # 模板详情
POST /api/v1/templates/{name}/expand       # 展开为可编辑工作流
POST /api/v1/apps/{id}/publish-template    # 发布草稿为新模板
```

### 工作流
```bash
POST /api/v1/applications                  # 创建应用
POST /api/v1/applications/{id}/draft       # 编辑草稿
POST /api/v1/applications/{id}/builds      # Builder 自动搭建
POST /api/v1/applications/{id}/tests/run   # 运行测试
POST /api/v1/applications/{id}/versions    # 发布版本
POST /api/v1/applications/{id}/runs        # 运行工作流
```

### Agent
```bash
POST /v1/agent-generations                 # 自动生成 Agent
POST /v1/sessions                          # 创建 Agent 会话
POST /v1/sessions/{id}/messages            # 发送任务
```

---

## 安全

- API 默认绑定 `127.0.0.1`，需 Bearer Token
- 密钥仅存在于 API 进程环境，不入库、不入容器
- 每个 Session 独立 Docker 容器，非特权，限制资源
- 沙盒 UID/GID 可通过 `.env` 配置（`SANDBOX_UID`, `SANDBOX_GID`）
- 生产部署建议接入独立 egress proxy 进行网络层域名过滤
