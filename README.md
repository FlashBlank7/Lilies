# 智能体/工作流生成平台

> **面向传统企业的 AI 工作流生成、编辑、运行与复用平台。莉莉丝是平台中的工作流 Builder 智能体。**

本平台对标 Dify 类可视化 AI 工作流产品，并增加由莉莉丝根据自然语言需求自动询问、搭建和修复工作流的能力。首要客户是尚未完成 AI 化、但有明确业务或技术流程需要自动化和智能化的传统企业。

产品最高意图见 [`docs/PRODUCT_NORTH_STAR.md`](docs/PRODUCT_NORTH_STAR.md)。Claude/Codex 式智能体架构、Harness、调度、Connector 和治理是支撑能力，不是产品本身或默认客户场景。

平台的核心洞察是：

> *人和人工作能力的差距，往往不是智力或经验的差距，而是工作流的差距。好流程让普通人产出好结果，坏流程让聪明人也寸步难行。*

平台把企业专家的做事方式和技术方案变成**可执行、可复用、可迭代、可组合**的积木工作流。工作流不仅包括 LLM，也应逐步覆盖数据接入、ML/DL、RAG、确定性规则、人工复核、Excel/文件工件和企业 API 交付。

---

## 架构哲学

```
传统 AI 平台:  问题 → [更好的 LLM/Agent] → 答案
                       ↑
                   关注"大脑"

本平台:        企业需求 → [莉莉丝自动搭建] → [可编辑工作流] → 业务结果/工件/API
                              ↑                  ↑
                         需求与缺口理解       LLM/ML/RAG/规则/人工
```

### 三个核心设计原则

**1. 工作流是模块，模块是工作流**
任何已发布的工作流都可以作为积木被其他工作流调用。通过 `$ref` 引用机制和 Version 锁定，工作流之间实现零耦合组合。这使系统具有分形能力——一个复杂工作流可以由若干子工作流构成，而每个子工作流本身就是可独立运行的完整单元。

**2. 模块间通过结构化文本通信 — "文本即智能"**
模块的输出不是黑箱 JSON，而是 LLM 天然理解的结构化文本。下游模块可以读取上游的自然语言结果继续推理。这降低了集成成本：不需要为每个模块定义精确的 Schema，语义靠 LLM 自己理解，结构靠 `$ref` 精确引用。

**3. 客户工作流结果 > 技术机制展示**
莉莉丝和 Agent Runtime 提供理解、规划、工具调用与修复能力，但平台价值最终由生成工作流是否解决真实企业问题决定。代码智能体、爬虫、Connector 和治理可以验证底座，不能代替工业 ML、RAG、数据与系统交付结果。

---

## 已实现的核心能力

### 积木系统 — 当前注册 46 个积木

| 类别 | 数量 | 示例 |
|------|------|------|
| 业务工作流积木 | 19 | LLM, If/Else, Iteration, Loop, Human Input, HTTP Request, Connector Action, Web Collection... |
| Agent 架构积木 | 26 | Context Assembler, Model Turn, Tool Executor, Permission Gate, Subagent Spawn, Budget Gate, Checkpoint/Resume... |
| 历史兼容积木 | 1 | Claude Agent (Legacy) |

当前目录在 Agent 架构、调度和集成底座上投入较多，但尚没有一等的 ML/DL 生命周期、RAG 检索链、工业数据处理和 Excel 工件积木或服务。这是明确的产品能力缺口，不能用通用 `llm`、`tool` 或 `http_request` 节点冒充。

### 模板市场 — 历史样例，不等于已验证行业能力

根目录 `templates/` 当前有 10 个可加载的历史模板，状态均为 `legacy_unverified`；另有 1 个无人机模板因 schema 不兼容无法加载。`data_analyzer` 只让 LLM 分析一段数据描述，并没有读取 CSV、训练模型或运行统计计算，不能作为真实数据工作流证据。

`BlockRegistry` 另提供 4 个代码级快捷模板：两个代码智能体、每日采集和客户系统嵌入。它们是当前技术能力样例，不构成传统企业场景组合。后续模板市场必须以真实企业项目、业务验收和明确证据等级为准。

### 莉莉丝 — AI 自动搭建工作流

输入自然语言需求 → 莉莉丝（Builder Team）自动：
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

### 方式 A: Docker Compose（推荐，前置条件最少）

**只需要 Docker。**

```bash
# 1. 配置 API Key
cp .env.example .env
# 编辑 .env，设置 DEEPSEEK_API_KEY（从 https://platform.deepseek.com 获取）

# 2. 一键启动（首次 3-5 分钟构建镜像）
./scripts/docker-up.sh

# 打开 http://localhost:8000/debug 即可测试
```

管理命令：
```bash
./scripts/docker-up.sh --status   # 查看运行状态
./scripts/docker-up.sh --logs     # 查看实时日志
./scripts/docker-up.sh --down     # 停止服务
```

### 方式 B: 本地开发（热重载）

需要 Python 3.12+、Node.js 20+、Docker。

```bash
# 1. 配置
cp .env.example .env
# 编辑 .env，设置 DEEPSEEK_API_KEY 和 API_TOKEN

# 2. 构建沙盒镜像
docker build --build-arg SANDBOX_UID=$(id -u) --build-arg SANDBOX_GID=$(id -g) \
  -t agent-platform-sandbox:latest -f Dockerfile.sandbox .

# 3. 安装依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 4. 启动（自动检查 Docker、端口、依赖）
./scripts/dev_platform.sh

# API: http://127.0.0.1:8001
# 调试页: http://127.0.0.1:8001/debug
# OpenAPI: http://127.0.0.1:8001/docs
```

本地开发请优先使用 `./scripts/dev_platform.sh` 同时启动 API 和 Studio。该脚本会把 Studio 代理和 Local Lilies assignment 回调都指向实际的 API host/port（默认 `http://127.0.0.1:8001`）；显式设置 `LILIES_PLATFORM_BASE_URL` 时则保留该值。不要手动把后端启动在另一个端口后再打开 Studio，否则前端代理或本地 daemon 回调会连到错误端口。

---

## 目录结构

```
Lilies/
├── platform/
│   ├── backend/src/agent_platform/
│   │   ├── api.py                FastAPI、SSE、模板 API
│   │   ├── blocks.py             46 积木定义、Schema、端口、图校验
│   │   ├── builder.py            莉莉丝 Builder Team（协调者+队友+任务+mailbox）
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
├── templates/                    历史工作流样例；加载不等于验证
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
