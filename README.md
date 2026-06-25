# Agent Platform

这是一个从原 TypeScript `src` 智能体核心重构而来的 Python 后端与原创可视化 Studio。它不是 CLI 包装器：模型循环、工具执行、权限、上下文压缩、隔离子智能体、持久队友、Skill、MCP、DAG 工作流、暂停恢复和版本发布都运行在后端。

当前只安装 DeepSeek Provider，但运行时依赖统一 `ModelProvider` 接口，后续可以添加 Anthropic、OpenAI 或本地模型适配器。

如果你想先理解产品和业务对象，而不是直接启动服务，请看 [业务逻辑与项目说明](docs/BUSINESS_LOGIC.md)。它梳理了 Application、Draft、Workflow、Agent、Build、Test、Version、Run、定时任务和发布规则之间的关系。

如果你想先搞清楚哪些目录是平台实现、哪些目录只是上游参考源码，请看 [项目目录结构](docs/PROJECT_STRUCTURE.md)。

如果你要继续修手动 Workflow Editor，先看 [手动编辑器测试计划](docs/MANUAL_EDITOR_TEST_PLAN.md)。后续重构 Dify/Claude Code 时建议预加载 [PRELOAD_PROMPTS.md](PRELOAD_PROMPTS.md)，里面沉淀了当前目录边界、启动、debug 和编辑器状态同步方法论。

## 已实现的闭环

1. 输入自然语言需求并创建 Application 草稿。
2. Claude 式协调器按需创建持久队友、任务依赖和验收条件。
3. 团队只能通过积木目录和增量 Draft API 添加节点、连接、Agent 和测试，不能一次性输出整张图或代码。
4. Python DAG 运行时执行 LLM、Claude Agent、Tool、条件、变量、HTTP、Iteration、Loop 和 Human Input 等 16 类积木。
5. 团队使用 DeepSeek、真实工具和沙盒运行验收；失败后继续修改，只有当前草稿 hash 全部通过才能发布。
6. 发布版本不可变并可立即运行；任何历史版本都能加载为新草稿继续人工微调。

## 启动

要求：Python 3.12、Docker daemon、可用的 DeepSeek API key。

### 推荐：一键本地开发启动

```bash
cp .env.example .env
# 编辑 .env，至少设置 DEEPSEEK_API_KEY 和 API_TOKEN

uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'

cd platform/frontend
npm install
cd ../..

docker build -t agent-platform-sandbox:latest -f Dockerfile.sandbox .

./scripts/dev_platform.sh --check-env
./scripts/dev_platform.sh
```

`--check-env` 只检查 `.env` 是否能被脚本正确读取，不会启动服务。正常时应输出：

```text
DEEPSEEK_API_KEY ok
API_TOKEN ok
```

`./scripts/dev_platform.sh` 会读取 `.env`、检查 DeepSeek/API token、检查端口冲突，并把前端代理指向同一个后端。本地开发标准端口固定为：

- API：`http://127.0.0.1:8001`
- Studio：`http://127.0.0.1:3000`

API 服务包含：

- 调试页面：`/debug`
- OpenAPI：`/docs`
- 健康检查：`/health`

如果提示端口已占用，先停止旧的 8001/3000 进程，再重新运行脚本。

前端开发模式默认使用 webpack dev server，避免 Next 16/Turbopack 在本地偶发 `Next.js package not found` panic 导致页面反复刷新。如果你想专门测试 Turbopack，可以进入 `platform/frontend` 后运行 `npm run dev:turbo`。

### 备用：手动分两个终端启动

终端 1，启动后端：

```bash
set -a; source .env; set +a
.venv/bin/uvicorn agent_platform.api:app --host 127.0.0.1 --port 8001
```

终端 2，启动前端。注意第二个终端也必须读取 `.env`，否则 `$API_TOKEN` 会是空值：

```bash
set -a; source .env; set +a
cd platform/frontend
AGENT_PLATFORM_URL=http://127.0.0.1:8001 API_TOKEN="$API_TOKEN" npm run dev -- --hostname 127.0.0.1 --port 3000
```

Studio 也支持在页面里输入 `.env` 中的 `API_TOKEN`；该 token 只保存在浏览器本地，并通过前端代理转发给后端。

### Docker Compose

Docker Compose 仍可用于容器化运行：

```bash
docker build -t agent-platform-sandbox:latest -f Dockerfile.sandbox .
docker compose up --build
```

Compose 默认暴露：

- API：`http://127.0.0.1:8000`
- Studio：`http://127.0.0.1:3000`

如果页面看起来正常但工具缺少 `WebSearch` 或显示 DeepSeek 未配置，通常是连到了旧的 8000 或 8001 进程；停止旧进程后用 `./scripts/dev_platform.sh` 重启。

工作区必须位于 `WORKSPACE_ROOT` 下。相对路径会以该目录为根解析，路径穿越会被拒绝。
当 API 本身运行在 Docker 中时，`WORKSPACE_HOST_ROOT` 必须是同一目录在宿主机上的绝对路径；`compose.yaml` 已默认使用 `${PWD}/workspaces`。

## API 示例

创建 Application 并让团队搭建：

```bash
APP_ID=$(curl -sS -X POST http://127.0.0.1:8001/api/v1/applications \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"Support Router","requirement":"按问题类型分流到不同 Claude Agent","mode":"workflow"}' \
  | jq -r .id)

curl -X POST "http://127.0.0.1:8001/api/v1/applications/$APP_ID/builds" \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"requirement":"按问题类型分流到不同 Claude Agent，并生成真实验收测试","auto_publish":true}'
```

积木、草稿、构建、测试、版本和运行接口位于 `/api/v1`；旧的独立 Agent/Session 接口继续保留在 `/v1`。

### 兼容的独立 Agent API

```bash
curl -X POST http://127.0.0.1:8001/v1/agent-generations \
  -H "Authorization: Bearer $API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "requirement": "生成一个能够分析并修复 Python 测试失败的智能体",
    "workspace_path": "demo",
    "auto_publish": true
  }'
```

使用返回的 `generation_id` 订阅事件：

```bash
curl -N "http://127.0.0.1:8001/v1/streams/$GENERATION_ID/events" \
  -H "Authorization: Bearer $API_TOKEN"
```

发布后创建会话并发送任务：

```bash
curl -X POST http://127.0.0.1:8001/v1/sessions \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"agent_id\":\"$AGENT_ID\",\"workspace_path\":\"demo\"}"

curl -X POST "http://127.0.0.1:8001/v1/sessions/$SESSION_ID/messages" \
  -H "Authorization: Bearer $API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"content":"运行测试，修复失败并重新验证"}'
```

## 真实验收

启动 API 后运行：

```bash
set -a; source .env; set +a
.venv/bin/python scripts/live_acceptance.py
.venv/bin/python scripts/live_workflow_acceptance.py
```

第一个脚本验证独立 Agent 修复真实 Python 测试；第二个验证团队增量搭建积木、真实测试、发布、运行和恢复为草稿。二者都会产生真实 API 费用，并且只从环境读取密钥。

普通测试不调用付费 API：

```bash
.venv/bin/pytest -q
.venv/bin/ruff check platform/backend/src/agent_platform tests scripts
cd platform/frontend && npm run lint && npm run build && npm audit
```

## 安全边界

- API 默认只绑定 `127.0.0.1`，`/v1` 接口要求 Bearer Token。
- DeepSeek 密钥仅存在于 API 进程环境，不写入数据库、事件或 Docker 工具容器。
- 每个会话使用独立容器；容器非特权、丢弃 capabilities、限制 CPU、内存和 PID，不挂载 Docker socket。
- 沙盒默认使用 UID/GID 10001；Linux 上如果工作区不可写，请把 `SANDBOX_UID`、`SANDBOX_GID` 设置为工作区所有者的非 root UID/GID。
- 默认网络策略为 `full`，满足安装依赖、Git、Web 和远程 MCP。`none` 使用 Docker 的无网络模式。
- `allowlist` 已进入 AgentSpec，但当前 Docker 执行器只传递策略元数据，尚未提供可证明的网络层域名过滤；部署到不受信任环境前应接入独立 egress proxy。

## 目录

```text
platform/backend/src/agent_platform/
  api.py            FastAPI、SSE、调试页面
  applications.py   增量草稿操作与校验
  blocks.py         16 类积木定义、Schema、端口和图校验
  builder.py        协调器、持久队友、任务/mailbox、测试修复与发布
  factory.py        需求到 AgentSpec、验证和发布
  runtime.py        agent loop、工具、压缩、子智能体
  workflow_runtime.py  DAG、分支、循环、暂停恢复和节点事件
  workflow_storage.py  Application、草稿、版本、Build 和 Run 持久化
  providers/        供应商无关接口和 DeepSeek 适配器
  tools/            核心工具和 MCP
  sandbox.py        Docker 会话执行器
  storage.py        SQLite WAL 与 JSONL 事件
platform/frontend/  Next.js 16 + React Flow 原创 Studio
references/claude-code/src/
                    原 Claude Code TypeScript 源码，只读迁移参考
```

原 Claude Code TypeScript 源码继续保留在 `references/claude-code/src/`，作为行为迁移和差异审查的参考。详见 [迁移矩阵](docs/MIGRATION_MATRIX.md)。
