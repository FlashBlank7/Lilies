# 智能体/工作流生成平台

> **让企业和个人用自然语言得到能真正运行的 AI 工作流。莉莉丝是平台的 Builder 智能体。**

平台对标 Dify 类可视化工作流产品，差异化在于：莉莉丝根据自然语言需求自动询问、搭建、测试和修复工作流。产出不是黑箱代码，而是一张**可编辑、可运行、可版本化**的工作流画布。

产品意图见 [`docs/PRODUCT_NORTH_STAR.md`](docs/PRODUCT_NORTH_STAR.md)，业务设计见 [`docs/BUSINESS_LOGIC.md`](docs/BUSINESS_LOGIC.md)。

## 核心洞察

> *人和人工作能力的差距，往往不是智力或经验的差距，而是工作流的差距。好流程让普通人产出好结果，坏流程让聪明人也寸步难行。*

平台把专家的做事方式变成**可执行、可复用、可迭代、可组合**的积木工作流。

### 三个设计原则

1. **工作流是模块，模块是工作流** — 已发布的工作流可以通过 `$ref` + 版本锁定被其他工作流当积木调用，系统具备分形组合能力。
2. **文本即智能** — 模块输出是 LLM 天然理解的结构化文本信封（`result` + `structured`），下游语义靠模型理解，结构靠 `$ref` 精确引用。
3. **能跑起来的工作流 > 机制展示** — 平台价值由生成的工作流是否解决真实问题决定。验收手段（测试、冒烟、校验）服务于这个目标，而不是反过来。

## 2026-08 lean-core 重构

平台曾经积累了大量治理、正式实验、协作开发和证据审计机器，验收压过了构建，简单工作流也难以生成。`refactor/lean-core` 分支做了一次大刀阔斧的裁撤：

- 删除 77 个后端治理/协作/桥模块（约 11 万行）与 190+ 战役脚本、审计测试
- Builder 去门禁：失败的构建**保留半成品草稿**供检查续作，不再回滚；交付缺口降级为警告
- 修复端口默认值缺陷：普通连线不再要求记住每种积木的端口名
- 历史战役文档全部归档至 [`docs/archive/`](docs/archive/)
- 测试全绿：456 passed / 0 failed（持续增长）

**重构后的成果三报告**：[阶段总结](docs/lean-core-stage-report.md)（砍了什么、建了什么）· [图文使用报告](docs/feature-usage-report.md)（八张真实截图）· [已知缺陷病历单](docs/known-defects.md)（九项全处置）。

### 重构后的新能力（2026-08）

- **会话式交付闭环**：会话页为默认入口——莉莉丝中文叙述、发布有系统徽章、纯工具轮自动翻译成人话动作行、长构建按工作段折叠；随时插话改单
- **监理**（第二智能体）：出卷（业主例子→验收规格）、监考（零模型成本机械验收+执行审计+血缘回溯）、陪看（业主语言解释）；验收不过一键"按验收单返修"
- **WaaS 使用界面**：每个工作流自动生成管理/极简/对话三件套界面，一条链接+访问码即交付；标注环节显隐可派生定制界面（服务端投影，零泄漏）
- **一键交付包**：客户交互包（README+链接+定义+产出）与专家材料包（素颜产出）两切面 zip
- **构建期动手能力**：Bash/Read/Write/Glob（Docker 沙盒、网络关闭）+ 平台内模型训练三工具（train/evaluate/promote，强制 held-out 评估与数据规模对账）
- **诚实失败纪律**：静默垃圾九防线（模板回声检测、思考耗尽自愈、$sum 全灭哨兵、未知配置键报错、数值锚定测试、玩具规模警告……）
- **成本工程**：构建历史滚动瘦身 + 前缀缓存纪律，单构建成本降至此前约 15%；缓存折扣入账
- **真实项目战绩**：珠宝报告 1 轮过、法律审查 6 轮过、电梯诊断（自训模型 0.883）、ERP 日报（盲测两头验收全绿）、电梯原题盲测（零辅导 628 数据点 / 96.36%）

保留的核心保证：端口/图结构校验、强制冒烟测试、发布前测试套件、修复循环与轮次预算、Docker 沙盒、revision 乐观锁、幂等键。

## 已实现的核心能力

### 积木系统

46 个积木分三类：**业务积木**（LLM、If/Else、Iteration、Loop、Human Input、HTTP、Connector、受控网页采集、知识检索、typed workbook、记录管线……）、**Agent 架构积木**（Context Assembler、Model Turn、Tool Executor、Permission Gate、Subagent Spawn、Budget Gate、Checkpoint/Resume……）以及 soft block 元积木。Agent 的内部循环被拆解为画布上可编排、可审计的一等节点。

### 莉莉丝自动搭建

自然语言需求 → 分析拆解 → 搜索积木目录 → 逐节点增量搭建（不允许直接输出整图 JSON，每条边实时按端口契约校验）→ 生成验收测试 → 运行、失败自修 → 通过后发布。

### 场景快速启动

`GET /api/v1/scenarios` 提供可一键应用的场景包（每日受控采集摘要、Codex 式工作区智能体、客户系统嵌入），应用后立即可编辑、可运行。

### 运行时

- DAG 拓扑执行、迭代/循环子图、Human Input 持久暂停与表单恢复
- Checkpoint/Resume（SQLite）、崩溃恢复、5/10 并发运行零交叉污染
- 定时触发 + 持久任务队列（租约、重试、审计事件、幂等回执）
- SSE 事件流全程可观测

### 安全

- API 默认绑定 `127.0.0.1` + Bearer Token；密钥仅存于 API 进程环境
- 每个会话独立非特权 Docker 容器，CPU/内存/PID 受限
- `MODEL_EGRESS_ENABLED=false` 默认阻断真实模型 HTTP，杜绝意外扣费

## 快速开始

### Docker Compose

```bash
cp .env.example .env      # 设置 DEEPSEEK_API_KEY；确认后再开 MODEL_EGRESS_ENABLED=true
./scripts/docker-up.sh
# 打开 http://localhost:8000/debug
```

### 本地开发

需要 Python 3.12+、Node.js 20+、Docker。

```bash
cp .env.example .env
docker build --build-arg SANDBOX_UID=$(id -u) --build-arg SANDBOX_GID=$(id -g) \
  -t agent-platform-sandbox:latest -f Dockerfile.sandbox .
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
./scripts/dev_platform.sh
# API: http://127.0.0.1:8001  Studio: 随脚本启动  OpenAPI: /docs
```

### 测试

```bash
python -m pytest tests -q
```

## 目录结构

```text
Lilies/
├── platform/
│   ├── backend/src/agent_platform/
│   │   ├── api.py                FastAPI、SSE、全部路由
│   │   ├── blocks.py             积木定义、端口契约、图校验
│   │   ├── builder.py            莉莉丝 Builder（协调者+队友+任务）
│   │   ├── workflow_runtime.py   DAG 执行、架构积木、checkpoint
│   │   ├── workflow_storage.py   草稿/版本/Build/Run、乐观锁
│   │   ├── scenarios.py          场景快速启动包
│   │   ├── template_store.py     模块注册表（workflow-as-module）
│   │   ├── knowledge_rag.py      SQLite RAG（ACL 前置、确定性）
│   │   ├── typed_workbook.py     确定性 XLSX 工件（sha256 + 血缘）
│   │   ├── record_pipeline.py    记录去重/归一/匹配
│   │   ├── durable_jobs.py       持久任务队列（租约/重试/审计）
│   │   ├── scheduler.py          定时触发
│   │   ├── connector_sdk.py      企业连接器 + OpenAPI 生成
│   │   ├── runtime.py            Agent 多轮循环
│   │   ├── sandbox.py            Docker 沙盒
│   │   └── providers/            ModelProvider 抽象 + DeepSeek
│   └── frontend/                 Next.js + React Flow Studio
├── templates/                    历史工作流样例
├── tests/                        行为测试（401 项，全绿）
├── docs/                         北极星、业务逻辑；archive/ 存历史
└── compose.yaml                  Docker Compose
```

## API 速览

```bash
# 工作流
POST /api/v1/applications                  # 创建应用
POST /api/v1/applications/{id}/draft       # 编辑草稿（乐观锁 + 幂等键）
POST /api/v1/applications/{id}/builds      # 莉莉丝自动搭建
POST /api/v1/applications/{id}/tests/run   # 运行验收测试
POST /api/v1/applications/{id}/versions    # 发布版本
POST /api/v1/applications/{id}/runs        # 运行工作流

# 场景与模板
GET  /api/v1/scenarios                     # 场景快速启动包
GET  /api/v1/templates                     # 模板列表
POST /api/v1/templates/{name}/expand       # 展开为可编辑工作流

# 需求补全与 Agent
POST /api/v1/requirements/complete         # 需求澄清
POST /v1/agent-generations                 # Agent Factory
POST /v1/sessions                          # Agent 会话
```

## 已知边界（诚实清单）

- 生成工作流的验收测试由莉莉丝自己生成，与客户真实需求可能存在偏移
- RAG 使用确定性哈希 embedding（零网络、可精确断言），语义召回有限，真实企业语料需要接入真模型 embedding
- typed workbook 只写不读，2MB 上限，无公式
- 本地莉莉丝（`../LiliesAgent/`）的平台桥已在重构中移除，待按更薄的 HTTP 合同重新接入
- Builder 会话 transcript 尚未落盘，构建失败的归因仍依赖事件流
