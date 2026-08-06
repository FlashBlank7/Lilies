# Lilies 平台功能、Builder 公开接口与积木手册全量报告

> 报告时间：2026-08-04（Asia/Tokyo）
> 对应代码：`v0.4.13`，提交 `410b7fa`，分支 `usabilityEnhence`，dirty worktree
> 报告对象：智能体/工作流生成平台、独立 Lilies Builder、平台生成的工作流
> 当前阶段任务：`V04-13-T01H`
> 证据口径：当前代码与公开 schema、当前进程实机启动、当前聚焦测试、最新锁定阶段报告；不把历史测试通过写成当前工作树全绿

## 1. 先说结论

当前平台已经不是一个“画几个方框”的原型。它已经具备完整工作流产品的主要骨架：自然语言需求入口、可视化画布、61 个公开积木、增量草稿编辑、结构和业务测试、运行与人工暂停、外部系统连接器、JSON/XLSX 工件、模型训练/导入/微调/部署/推理/漂移、RAG、事件和定时任务、发布版本、客户运行页、治理台，以及让独立 Lilies 通过受限 HTTP API 搭建工作流的黑箱接口。

但需要同时看到四个限制：

1. 当前开发 API 实际列出 11 个应用，其中 1 个有 active version 1；浏览器在首页首次 `domcontentloaded` 时曾短暂显示 `0 个应用`，那是异步列表尚未回填的加载帧，不能当成存储为空。这个现象也说明首页需要更明确的 loading 状态，避免把“尚未加载”显示成“没有应用”。
2. 当前模型配置存在，但 `MODEL_EGRESS_ENABLED=false`，所以浏览平台不消耗模型 Token，AI 生成和 LLM 节点也不会真的外呼模型。
3. 当前 `local_agent_enabled=false`，同级 `LiliesAgent` 没有连接到这次启动的 8001 平台；平台启动与 LiliesAgent 在线是两件事。
4. 当前聚焦回归为 `78 passed, 2 failed`。两项红灯分别是合同版本漂移测试仍把版本 4 当作“新版本”，以及 XLSX 测试仍期待旧工件路径；当前实现已经采用合同版本 4 和按 run 隔离的工件路径。它们看起来是 dirty worktree 中实现与测试预期未同步，但在修复并复跑前，本报告不会声称当前工作树全绿。

产品层面的完成情况也不能写成“v0.4.13 已完成”：阶段任务 A–G、K–L 已在各自声明上限内完成；T01H 的 r8 fresh-final 六项目分母仍是 `0/6`，I、J、M、N 尚未完成。

## 2. 当前实机运行状态

| 项目 | 当前状态 | 含义 |
| --- | --- | --- |
| Studio 前端 | 已启动：`http://127.0.0.1:3000` | 可以查看需求入口、应用列表、Studio、治理和协作入口 |
| 平台 API | 已启动：`http://127.0.0.1:8001` | `/health` 返回 `status=ok`、`current_code_ready=true` |
| API 规模 | 238 条 path、264 个 HTTP operation | 这是平台完整开发/管理 API，不等于 Lilies 全部可见 |
| Builder 公开 API | 17 个受 assignment 限定的 operation | Lilies Builder 真正获得的工作流搭建接口 |
| 公开积木 | 61 个 | 35 个业务工作流积木，26 个 Agent 架构积木 |
| 模型供应 | DeepSeek 已配置，模型外呼关闭 | 浏览和确定性检查不消耗模型 Token；真实模型调用失败关闭 |
| Local Lilies | 功能开关关闭、未发现 daemon | 需要单独启动同级 `../LiliesAgent` 并显式配对，发现不等于授权 |
| 当前应用数据 | API 实际返回 11 个应用，其中 1 个有 active version 1 | 首页首个加载帧曾短暂显示 0，异步请求完成后才是权威应用数 |
| Docker | Docker 可用；项目一旧客户环境仍有 7 个容器运行 | Paperless、InvenTree、各自数据库/缓存和 worker；不是本次前端启动新建的 |
| 另一实验 API | `http://127.0.0.1:18100` 仍在运行 | 这是既有 T01H 实验进程，与 8001 开发平台分开 |
| Git 状态 | dirty | 本次只新增本报告，不覆盖或清理既有修改 |

首页实机可见的主流程是：

```text
写清业务结果
  → AI 需求补全或直接保存草稿
  → 显式选择 Local Lilies / 旧 Builder 路线
  → 进入可视化 Studio 检查和编辑
  → 运行验收
  → 试运行并查看 Trace / 人工等待 / 权限等待
  → 发布不可变版本
  → 在 Customer Runtime 中使用
```

这说明平台的产品主线是“需求到可编辑、可测试、可发布工作流”，不是把实验管道或独立 verifier 当成客户产品。

## 3. 平台整体架构：工作流其实是什么

工作流本质上是一份持久化配置，而不是十几份需要人理解的编译产物。核心配置包括：

- 应用元数据和原始需求；
- 节点列表；
- 节点之间的连线和端口；
- 每个节点的配置；
- Agent、Skill、MCP 和工具配置；
- 验收测试；
- 能力合同与发布策略；
- 草稿 revision 和不可变发布 version。

平台在保存或执行前会做 schema 校验、端口校验、引用解析、安全校验和运行计划准备，这些属于运行时内部处理。它不要求 Builder 自己维护一套“验收计划编译器、节点编译器、连线编译器、测试编译器”。此前 LiliesAgent 出现的长链条：

```text
验收计划 → 能力目录 → 业务蓝图 → 本地节点/连线/测试编译
→ 嵌套蓝图 → 整体提交 → 再测试
```

主要是智能体框架在平台 Draft 之外又造了一层本地中间表示，不是平台工作流必须如此复杂。平台公开 API 已支持一次一个 `DraftOperation` 的增量搭建；更合理的 Builder 工作方式是：

```text
读需求和积木手册
→ 增加一个可验证的小片段
→ 平台立即校验并保存 revision
→ 根据结构/运行反馈只修当前问题
→ 加测试、完整运行、发布
```

## 4. Lilies Builder 实际收到什么

### 4.1 不是把 264 个 API 全塞给 Lilies

平台完整 OpenAPI 有 264 个 operation，其中包括管理员、Studio、治理、模型管理、连接器生成、实验 Harness、本地桥和开发协作接口。普通 Builder 不应看到或调用全部接口。

Builder assignment 只授予一份经过角色和应用过滤的公开合同。LiliesAgent 将合同中的 operation 转成模型工具定义：

```json
{
  "name": "platform_draft_apply",
  "description": "Apply exactly one incremental public DraftOperation.",
  "input_schema": {
    "type": "object",
    "properties": {
      "application_id": {"type": "string", "format": "uuid"},
      "expected_revision": {"type": "integer", "minimum": 0},
      "idempotency_key": {"type": "string", "minLength": 16, "maxLength": 128},
      "op": {
        "enum": [
          "add_node", "update_node", "remove_node",
          "add_edge", "remove_edge", "set_metadata",
          "upsert_agent", "add_test", "remove_test",
          "set_capability_build_contract"
        ]
      },
      "data": {"type": "object"}
    },
    "required": ["application_id", "expected_revision", "idempotency_key", "op", "data"],
    "additionalProperties": false
  }
}
```

这里最重要的设计是：一次调用只做一个增量修改；`expected_revision` 防止覆盖别人刚保存的草稿；`idempotency_key` 防止网络重试重复增加节点或重复写客户系统。

### 4.2 17 个 Builder 公开操作

#### A. 发现平台能力

| 操作 | HTTP | Lilies 用它做什么 |
| --- | --- | --- |
| `platform_contract_get` | `GET /api/v1/lilies/platform-contract` | 读取当前 assignment 能用的操作、schema、错误类型、合同摘要和边界 |
| `platform_block_search` | `GET /api/v1/lilies/blocks` | 按关键词和积木类型搜索最多 50 条简要结果，不一次吞下所有大 schema |
| `platform_block_get` | `GET /api/v1/lilies/blocks/{block_type}` | 精确读取一个积木的端口、配置 schema、示例、反例和组合约束 |
| `platform_tool_catalog` | `GET /api/v1/lilies/tools` | 读取当前任务允许工作流调用的工具和连接器操作合同 |

#### B. 创建与检查应用

| 操作 | HTTP | Lilies 用它做什么 |
| --- | --- | --- |
| `platform_application_create` | `POST /api/v1/lilies/applications` | 在 assignment 名下创建一个空应用，设置名称、需求、工作流/聊天模式和交付模式 |
| `platform_application_get` | `GET /api/v1/lilies/applications/{id}` | 读取应用、草稿 revision、已发布版本摘要 |
| `platform_draft_inspect` | `GET /api/v1/lilies/applications/{id}/draft` | 读取草稿节点、连线、测试、验证错误和当前 revision |
| `platform_draft_apply` | `POST /api/v1/lilies/applications/{id}/draft` | 一次增加/修改/删除一个节点、连线、测试、Agent 或元数据 |

#### C. 测试、运行和人工恢复

| 操作 | HTTP | Lilies 用它做什么 |
| --- | --- | --- |
| `platform_tests_run` | `POST /api/v1/lilies/applications/{id}/tests/run` | 运行完整验收套件，返回逐用例结果和修复目标 |
| `platform_run_start` | `POST /api/v1/lilies/applications/{id}/runs` | 用草稿或指定发布版本启动真实运行 |
| `platform_run_get` | `GET /api/v1/lilies/runs/{run_id}` | 查看运行中、成功、失败、取消或等待人工/权限状态，以及输出和工件 |
| `platform_run_resume` | `POST /api/v1/lilies/runs/{run_id}/resume` | 为等待中的 `human_input` 提交类型化表单值并继续运行 |
| `platform_run_cancel` | `POST /api/v1/lilies/runs/{run_id}/cancel` | 显式停止运行 |
| `platform_trace_get` | `GET /api/v1/lilies/runs/{run_id}/trace` | 分页读取脱敏后的结构化执行轨迹，不读取数据库或私密思维链 |
| `platform_artifact_read` | `GET /api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}` | 按字节分块读取已登记、摘要校验和路径受限的真实工件 |

#### D. 外部副作用与发布

| 操作 | HTTP | Lilies 用它做什么 |
| --- | --- | --- |
| `platform_connector_authorization_issue` | `POST /api/v1/lilies/applications/{id}/connector-authorizations` | 对一个确切 payload 签发一次性、短时、任务策略限定的写入授权 |
| `platform_publish` | `POST /api/v1/lilies/applications/{id}/versions` | 在 Builder 明确决定后发布不可变版本；平台仍检查结构、权限和执行安全 |

### 4.3 请求认证与关联字段

每个调用不是只带一个万能管理员 Token，而是绑定具体 assignment 和会话：

| 字段 | 作用 |
| --- | --- |
| `Authorization: Bearer <task credential>` | 只能获得任务授予的 scope、应用和连接器权限 |
| `X-Lilies-Assignment-ID` | 把调用绑定到任务 |
| `X-Lilies-Session-ID` | 把调用绑定到一次可恢复的 Builder 会话 |
| `X-Lilies-Tool-Call-ID` | 把 HTTP 调用与 Lilies 会话中的具体工具调用关联 |
| `X-Lilies-Idempotency-Key` | 对重试进行 exactly-once 保护；相同 key 不允许换 payload |
| `X-Lilies-Contract-Digest` | 防止 Lilies 按旧 schema 调用已变化的平台 |

这就是“平台反馈”从哪里来：不是 Codex 直接看数据库，而是平台把当前操作的公开、脱敏、结构化结果返回给调用者。

### 4.4 统一返回结构

所有操作都返回同一类 envelope：

```json
{
  "ok": false,
  "operation": "platform_draft_apply",
  "request_id": "...uuid...",
  "status_code": 409,
  "contract_digest": "sha256:...",
  "data": {},
  "error": {
    "code": "request_conflict",
    "message": "...可安全展示的具体原因...",
    "retryable": false,
    "failure_owner": "lilies",
    "expected": {"revision": 12},
    "actual": {"revision": 11},
    "evidence_ref": null
  },
  "evidence_refs": []
}
```

`failure_owner` 明确区分 `lilies`、`user_permission`、`task_author`、`environment`、`platform`。这使智能体可以判断应该修工作流、请求用户权限、等待环境还是报告平台缺口，而不是看到一个笼统的 500 后整图重来。

### 4.5 Lilies 不会自动获得的内容

Builder 合同明确不等于平台开发权限。正常 Builder 不会因为能调用公开接口就自动获得：

- 平台源码和 Git 历史；
- 平台数据库；
- 其他 assignment 的应用；
- 开发者工作区；
- 隐藏 Seed、oracle、expected/actual 差异；
- 未授予的连接器操作、网络、秘密或写入预算；
- 管理员治理、内部 Harness 和凭据签发能力。

真实业务中如果客户允许读数据库，可以通过一个正式数据库 Connector 或受限开发 assignment 授权；“不能读数据库”是当前黑箱 Builder assignment 的边界，不是 Lilies 这个通用智能体永久没有数据库能力。

## 5. 积木手册到底长什么样

积木搜索只返回简要目录。Lilies 决定使用某个积木后，再调用 `platform_block_get` 读取完整手册。手册不是一段散文，而是机器可校验 schema 加人类说明：

| 字段 | 内容 |
| --- | --- |
| `type / title / description` | 稳定类型名、显示名和一句话用途 |
| `category` | input、model、agent、logic、transform、integration、output |
| `block_kind` | `business_workflow`、`agent_architecture` 或历史兼容类别 |
| `summary` | 更具体的行为和保证 |
| `when_to_use` | 什么时候应该用 |
| `input_ports / output_ports` | 端口名、值类型、是否必需、是否允许多个连接 |
| `config_schema` | 严格 JSON Schema：必填字段、类型、枚举、范围、是否允许额外字段 |
| `examples` | 连接方式和配置示例 |
| `anti_patterns` | 明确禁止的误用 |
| `common_errors` | 常见错误及其原因 |
| `claude_architecture_mapping` | Agent 架构积木与参考架构概念的对应关系 |
| `composability_constraints` | 与其他积木连接和运行时语义的硬约束 |

### 5.1 代表性手册：业务记录匹配

`record_match` 的自然语言合同是：把一条来源记录与有限候选比较，使用加权精确、忽略大小写或数值条件计算可解释分数，同时执行硬冲突检查。

- 必填配置：`source`、`candidates`、`conditions`；
- 条件最多 32 个，支持字段路径、比较器、权重和 required；
- 可另设最多 32 个 `conflict_checks`；
- 输出状态只有 `matched`、`not_found`、`ambiguous`、`conflict`；
- 只有 `matched` 才会返回非空 `match`；
- 候选按分数和原始索引稳定排序；
- 手册明确禁止“模糊或冲突时直接取第一个候选”。

这不是让 LLM 自由猜匹配，而是把匹配、歧义和冲突变成可测试的确定性业务逻辑。

### 5.2 代表性手册：人工复核

`human_input` 会在生产运行中持久暂停，等待一个类型化表单；测试时可以用按节点 ID 指定的 `simulated_human_inputs` 做无人值守验收。

- 字段支持 string、number、boolean、object、array、file、file_list 等类型；
- 生产运行必须通过公开 `run_resume` 继续；
- 测试模拟输入不会扩大生产运行权限；
- 手册明确禁止“为了让测试跑完而删掉真实人工复核节点”。

### 5.3 代表性手册：客户系统写回

`connector_action` 一次只执行一个已登记、带版本的 Connector 操作。

- 必填：连接器 ID、操作 ID、tenant、actor、roles、profile、payload 和幂等键；
- 平台同时限制租户、操作、网络、payload schema、写预算和授权；
- 读数据用 `response`，审计和追踪用 `receipt`；
- 写操作可以使用一次性确切 payload 授权；
- 只有连接器合同声明可安全重放时才允许配置重试；
- 手册禁止发明不存在的 endpoint、字段、操作或 authorization id。

### 5.4 代表性手册：真实 JSON 和 Excel 交付物

`typed_json_artifact` 会写出 canonical JSON，注册摘要、媒体类型、幂等结果和 lineage；拒绝非 JSON 类型、NaN/Infinity、路径穿越和同名不同内容。

`typed_workbook` 会生成真实 XLSX，而不是返回一段“表格说明”：

- 最多 16 个 sheet；
- 每个 sheet 最多 128 列、5000 行；
- 列类型支持 string、integer、number、boolean、date、datetime；
- 默认拒绝看起来像公式的文本，不执行任意公式；
- 标识符超过 Excel 精确数字范围时必须用字符串；
- 文件只写在当前 run 的工件目录，带内容摘要和来源 lineage。

### 5.5 代表性手册：已部署模型推理与 RAG

`deployed_model_inference` 只调用一个已经获批的不可变部署版本，返回概率、预测标签、置信度、模型 ID/版本/摘要、模型卡和评估指标；它明确不在工作流运行时训练、提升或回滚模型。

`knowledge_retrieval` 先按调用者角色剔除无权访问的来源，再进行混合检索。配置包含索引名、查询、角色、`top_k` 和最低分；输出包含结果、数量、ACL 决策、被禁止 chunk 数和模型版本。

当前这两个手册的 runtime schema 已经明确，但 examples、anti-patterns 和 common-errors 仍使用较通用的模板语言，说明“功能存在”不等于“手册质量已经达到所有 Builder 都容易用”的水平。

## 6. 61 个公开积木全量目录

### 6.1 业务工作流积木：35 个

#### 输入与触发：4 个

| 积木 | 自然语言用途 |
| --- | --- |
| `start` | 定义一次工作流运行从用户或调用方接收哪些类型化输入 |
| `human_input` | 在运行中暂停，向人展示类型化表单，收到决定后恢复 |
| `schedule_trigger` | 按已发布版本中的时间计划触发运行 |
| `event_subscription_trigger` | 订阅外部事件，在事件到达时启动工作流 |

#### 逻辑控制：6 个

| 积木 | 自然语言用途 |
| --- | --- |
| `if_else` | 根据显式条件选择不同分支 |
| `iteration` | 对有限集合逐条执行一个子流程并汇总每条结果 |
| `loop` | 在有界停止条件下重复执行，避免无限循环 |
| `question_classifier` | 把问题分到预先定义的类别，再路由到对应分支 |
| `durable_event_timer` | 创建可恢复的长期等待计时器，进程重启后仍能继续 |
| `replenishment_planner` | 在 MOQ、批量、容量、预算等约束下计算补货方案并解释不可行原因 |

#### 数据转换与质量：10 个

| 积木 | 自然语言用途 |
| --- | --- |
| `record_collection_normalize` | 把不同列表响应整理成统一、有限的记录集合 |
| `record_deduplicate` | 按多个字段路径稳定去重，并为原记录和重复记录保留 receipt |
| `record_match` | 对候选做加权匹配、歧义判断和硬冲突检查 |
| `json_schema_validate` | 用明确 JSON Schema 校验数据类型、必填字段和结构 |
| `parameter_extractor` | 从输入中抽取定义好的参数 |
| `regex_extract` | 用确定性正则提取格式稳定的字段 |
| `template_transform` | 把结构化输入映射为预定输出结构或文本 |
| `variable_assigner` | 创建或更新显式变量 |
| `variable_aggregator` | 汇总多个上游值成为统一结果 |
| `collection_digest` | 为集合生成稳定摘要，用于重放保护、比较和 lineage |

#### 外部集成：5 个

| 积木 | 自然语言用途 |
| --- | --- |
| `connector_action` | 按租户、操作、payload、授权和幂等策略读写客户系统 |
| `http_request` | 调用允许的 HTTP 接口，适合合同清楚但未注册为完整 Connector 的场景 |
| `tool` | 调用平台注册的通用工具 |
| `web_collection` | 在受控网络和内容边界内采集网页证据 |
| `knowledge_index_sync` | 把新增、更新或删除的企业内容同步到知识索引 |

#### 模型与知识：6 个

| 积木 | 自然语言用途 |
| --- | --- |
| `llm` | 调用配置的语言模型执行生成或理解任务 |
| `knowledge_retrieval` | 先做 ACL 权限过滤，再检索证据 |
| `grounded_answer` | 仅根据检索证据生成带引用答案，证据不足时安全拒答 |
| `deployed_model_inference` | 调用已获批的不可变表格模型部署版本 |
| `deployed_forecast` | 调用已部署的预测模型，输出预测和版本证据 |
| `model_drift_monitor` | 比较当前数据或表现与基线，产生漂移和重训建议 |

#### 输出与交付：4 个

| 积木 | 自然语言用途 |
| --- | --- |
| `answer` | 为聊天或可读结果生成最终回答出口 |
| `end` | 明确定义工作流最终结构化输出 |
| `typed_json_artifact` | 生成真实、可下载、摘要校验、带 lineage 的 JSON 文件 |
| `typed_workbook` | 生成真实、类型化、安全、可下载的 XLSX 文件 |

### 6.2 Agent 架构积木：26 个

这些不是每个企业工作流都应该摆在主画布上的业务步骤，而是构建 Codex/Claude 类复杂 Agent 时可检查、可替换的运行机制。

| 积木 | 自然语言用途 |
| --- | --- |
| `model_turn` | 执行一轮模型判断或生成 |
| `context_assembler` | 把任务、消息、工具结果和其他上下文组装成当前模型输入 |
| `context_compactor` | 在上下文过大时压缩历史，同时保存恢复摘要 |
| `conversation_memory` | 保存和读取受治理的会话记忆 |
| `workspace_context_injector` | 把授权文件或工作区内容注入上下文 |
| `skill_loader` | 按任务选择并加载 Skill 指令 |
| `capability_registry` | 声明和发现当前 Agent 能使用的能力 |
| `mcp_gateway` | 通过 MCP 协议发现和调用外部工具服务 |
| `tool_call_router` | 把模型提出的工具调用路由到正确执行器 |
| `tool_executor` | 在权限、沙箱和取消边界内真正执行工具 |
| `tool_result_normalizer` | 把不同工具返回整理成模型可以稳定理解的结构 |
| `permission_gate` | 对需要用户或策略批准的动作暂停并等待授权 |
| `sandbox_boundary` | 显式限定文件、进程和运行环境边界 |
| `retry_error_classifier` | 区分可重试、权限、工具错误和致命错误，选择恢复路径 |
| `checkpoint_resume` | 保存可恢复检查点，并在进程重启或中断后继续 |
| `cancellation_point` | 在长任务安全点检查取消并停止后续副作用 |
| `budget_gate` | 在模型、工具、时间或成本超预算前停止 |
| `round_limit` | 限制模型—工具循环轮数，避免无界循环 |
| `stop_continue_controller` | 根据完成、失败、等待和预算状态决定继续还是结束 |
| `dependency_gate` | 等待前置任务或证据满足后再执行 |
| `mailbox_wait_wake` | 让 Agent 等待异步消息并被新任务唤醒 |
| `task_dispatcher` | 把任务分配到适合的执行者或子流程 |
| `subagent_spawn` | 在显式授权和预算下创建子 Agent |
| `event_recorder` | 持久记录可观察动作、状态变化和证据引用 |
| `hook_point` | 为明确生命周期事件提供受控扩展点 |
| `soft_block` | 在非致命风险下暂停、降级或请求补充，而不是直接崩溃 |

### 6.3 积木膨胀风险与当前控制方式

61 个积木并不都应该同时压到普通业务用户眼前。当前代码已经有 `block_kind`，能区分 35 个业务积木和 26 个 Agent 架构积木，前端也能按类别搜索和分组。这是正确基础，但还没有完全解决认知负担。

建议产品层保持三层：

1. 默认只显示与当前需求相关的少量业务积木；
2. 复杂组合优先沉淀为可安装模块或子工作流，不把每个领域规则都做成原子积木；
3. Agent 架构积木进入“高级/Agent 架构”能力包，普通企业流程不默认展开。

能力放置应遵循：通用且原子的才成为积木；多步组合成为模块；跨工作流的状态、权限、模型部署、索引和调度属于平台服务；客户 endpoint、字段映射和规则留在 Connector 或工作流配置中。

## 7. 平台模块和功能全量地图

### 7.1 客户产品核心

| 模块 | 当前功能 | 当前边界 |
| --- | --- | --- |
| 需求入口 | 自然语言需求、客户/输出/验收/细节就绪度、AI 选项式补全、Quick/Guided/Governed 交付模式 | 当前模型外呼关闭时不能运行真实 AI 补全 |
| 应用管理 | 创建、列表、详情、草稿 revision、版本摘要、删除/清理开发 smoke 数据 | 当前开发 API 有 11 个应用；首页需要把 loading 与真正空列表区分开 |
| Engineer Studio | 可视化节点和连线、拖拽积木、端口校验、普通表单配置和专家 JSON 配置、测试、发布 | 复杂大图的自动布局和引导仍可改进 |
| Customer Runtime | 面向最终用户的输入表单、运行、状态、输出、人工恢复；隔离开发内容 | 当前有 1 个 active version 可作为已发布版本入口，其余主要是草稿 |
| 发布与版本 | 发布决定、警告确认、不可变 version、恢复旧版本、runtime definition | Quick/Guided 以提示为主；Governed 可使用显式硬门 |

### 7.2 工作流存储、执行与可靠性

| 模块 | 当前功能 |
| --- | --- |
| Workflow Storage | 保存工作流、节点、连线、测试、Agent 和 revision；发布不可变快照 |
| Workflow Runtime | 解析引用、执行分支/循环/迭代/嵌套工作流、错误策略、重试、人工等待、输出和 Trace |
| Durable Jobs | 长任务状态、事件、receipt、retry、resume、cancel |
| Scheduler | 定时配置、应用计划状态、显式触发 |
| Event Automation | 事件订阅、事件状态、持久计时器 |
| Idempotency | 草稿修改、运行、外部写入和工件均可使用稳定幂等键，冲突 payload 会被拒绝 |
| Cancellation | 普通 run、build、durable job 和 Local Lilies assignment 均有显式取消路径 |

### 7.3 测试与验收

| 模块 | 当前功能 |
| --- | --- |
| Workflow Tests | 输入、模拟人工响应、结构要求、工具调用要求、字段断言、强制用例 |
| Evaluation Harness | 评估 profile、环境、应用计划、测试生成/应用、评估运行 |
| Repair Preview | 测试失败后生成修复预览，再由用户或 Builder 明确应用 |
| Evidence Levels | 可表达 H0–H5、mock/contract/sandbox/live/production observation 和 claim ceiling |
| Builder Benchmark | 历史、单次评估和 suite 评估 |
| Platform Harness | 任务、lease、队列、worker heartbeat/supervision/process manager；这是可靠执行基础设施，不是客户业务工作流本身 |

### 7.4 连接器与客户系统

| 模块 | 当前功能 |
| --- | --- |
| Connector Manifest | 注册带版本的连接器与 operation 合同 |
| OpenAPI 生成 | 从公开 OpenAPI 材料生成连接器候选，保留 generation 和 descriptor |
| 自动合同测试 | 生成 contract case、运行 contract run、验证读写契约 |
| Binding / Policy | 把连接器绑定租户、profile、角色、网络、秘密和写策略 |
| Authorization | 写入和补偿使用短时、确切 payload、单次消费授权 |
| Execution | 执行、事件、receipt、回读、补偿、callback 和演练 |
| Emergency Stop | 可按连接器版本和租户停止写入 |

平台的目标是让接口发现、认证、schema 对齐、写回和回执都由平台/Builder 完成，而不是实验人员预写项目专用 adapter。当前 API 形态已经支持这一目标，但六个 fresh-final 项目尚未完成，所以不能把“接口模块存在”写成“所有真实客户接口都已自动适配成功”。

### 7.5 数据、文件和工件

| 模块 | 当前功能 |
| --- | --- |
| 类型化输入 | Start 和 Human Input 可声明 string、number、boolean、object、array、file、file_list |
| 数据处理 | schema 校验、记录归一化、去重、匹配、模板转换、聚合和摘要 |
| JSON 工件 | canonical JSON、摘要、媒体类型、lineage、路径和大小边界 |
| XLSX 工件 | 类型化 sheet/column/cell、安全公式策略、摘要和 lineage |
| Artifact Registry | 每个 run 登记工件，Builder 可通过受限 API 分块读取 |
| Web Collection | 在网络和内容边界内收集网页证据 |

目前没有证据表明平台已经提供一个同等成熟、公开的通用 PDF/OCR 原子积木族；这类需求应通过现有工具/连接器/模块承载，或在真实项目证明通用缺口后补能，不能仅凭路线图写成已完成。

### 7.6 ML、预测和部署

| 模块 | 当前功能 |
| --- | --- |
| Tabular Model | train、import、list/read version、fine-tune、evaluate |
| Model Deployment | 读取 deployment、promote、rollback、predict、drift |
| Forecast Model | train、import、version、fine-tune、evaluate |
| Forecast Deployment | promote、rollback、predict |
| 工作流推理 | `deployed_model_inference`、`deployed_forecast` |
| 模型监控 | `model_drift_monitor`，工作流可根据漂移建议人工复核或触发训练链 |

这套分层符合“训练/导入/微调/评估/部署在模型管理层，工作流运行时只调用已部署模型”的产品逻辑。当前支持模型导入，但没有看到一个已经验收的任意外部模型市场搜索器；“去外部模型库自动搜索候选”仍应标为待真实验证的能力，而不能等同于 import API。

### 7.7 企业 RAG

| 模块 | 当前功能 |
| --- | --- |
| Knowledge Index | 创建/读取索引、同步内容、检索、生成答案 |
| Embedding | 独立 embedding invoke 接口 |
| 权限 | 检索前按 principal roles 过滤来源，记录 forbidden chunk count |
| 工作流 | 索引同步、ACL-first retrieval、grounded answer |
| 证据 | 结果可以保留引用、ACL 决策和模型版本 |

接口与积木已存在；BookStack fresh-final 企业 RAG 项目仍未按 r8 完成，因此当前不能宣称对所有更新、删除、权限继承、held-out query 已取得最终业务通过。

### 7.8 模块、模板和场景

| 模块 | 当前功能 |
| --- | --- |
| Capability Modules | 创建模块、发布版本、插入应用、登记 evidence、验证版本 |
| Templates | 分类、建议、读取、展开、评分、发布为模板、从会话提取、merge check/merge |
| Adaptive Monitoring | 模板刷新、调度、单次运行 |
| Scenario Catalog | 列出场景、应用场景到工作流 |
| Module Protocol | 校验模块协议 |

这部分是控制积木膨胀的关键：领域能力优先作为模块/模板安装，而不是每出现一个项目字段就新增平台原子积木。

### 7.9 权限、安全和治理

| 模块 | 当前功能 |
| --- | --- |
| Permission Broker | 工具、运行和人工审批的显式权限请求与处理 |
| Sandbox | 工作区与工具执行隔离、路径和进程边界 |
| Secrets | 平台 secret 创建/读取边界，Connector 使用 secret 引用而非把值写进图 |
| Governed Memory | 创建、读取、撤销和过期受治理记忆 |
| Governance Console | overview、tasks、usage、reliability、durable jobs、connectors、traces、policy、evidence、alerts |
| Execution Policy | 发布和运行时冻结策略快照，避免执行时悄悄扩大权限 |
| Trace Redaction | Builder 只读脱敏结构化 Trace，不返回凭据或私密思维链 |

当前并不声明客户生产级多租户 IAM、安全认证、跨设备同步或外部合规认证已经完成。

### 7.10 Lilies 本地智能体与协作

| 模块 | 当前功能 | 当前运行状态 |
| --- | --- | --- |
| Standalone LiliesAgent | 同级独立仓库、daemon/CLI、私有状态、会话、权限、Token 观测、macOS 应用 | 历史 T01K/L 在 scoped evidence floor 完成；本次没有启动 |
| Safe Discovery | 同用户、0600、非 symlink 发现记录；loopback health 和指纹核对 | 本次 `local_agent_enabled=false` |
| Pairing | 一次性码显式配对，发现不授予权限 | 本次未配对 |
| BuildAssignment Bridge | 平台创建 assignment，私密投递 task credential，关联应用/build/session | 功能存在，本次未运行 assignment |
| Observable Session | 展示消息、工具调用、错误、草稿、run、trace、工件、Token 和阶段；不展示私密 chain-of-thought | Developer Studio 有界面和 API |
| Collaboration | 人工或自主交接、报告、lease、response、独立复验 | 属于开发/实验基础设施，不进入企业工作流成功分母 |

### 7.11 正式实验和独立验证基础设施

平台仓库还包含题包 revision、过滤工作区、assignment broker、归档、hidden seed、独立 verifier、补能报告和因果台账。这些功能用于证明 Builder 在不知道隐藏答案时能交付，不是客户使用平台时必须经历的“额外发布门”。

它们在历史上给 Builder 带来了较大理解和运维负担，后续重构应保持：

- 客户工作流的创建、运行和发布路径简单；
- 实验隔离和隐藏验收放在外层 Harness；
- 不把实验 fresh-empty、archive、seed 或 verifier 条件写进普通客户的产品流程；
- verifier  disagreement 不能自动被描述成平台无法交付，必须与公开运行和客户系统回读对账。

## 8. 前端目前有哪些页面

| 页面 | 入口 | 主要用途 |
| --- | --- | --- |
| 首页/应用列表 | `/` | 需求输入、AI 补全、交付模式、构建路线、Local Lilies 状态、应用列表 |
| Engineer Studio | `/applications/{id}` | 画布、积木库、节点配置、Agent、连接器、测试、评估、调度、发布、Local Lilies 构建 |
| Customer Runtime | `/runtime/{id}` | 最终用户填写输入、运行工作流、看业务输出和人工等待 |
| Governance Console | `/governance` | 任务、使用量、可靠性、连接器、trace、策略、能力证据和告警 |
| Collaboration Studio | `/developer/collaboration` | 追踪 Lilies/Codex 开发协作、公开工具链、报告、验证和处理人 |

当前首页顶部仍显示 `Foundry`，同时产品文档和 Builder 名称使用 Lilies。这是品牌/命名一致性债务，不影响运行，但会增加非技术用户理解成本。

## 9. 当前验证证据与不能夸大的地方

### 9.1 本次直接核对

- 前端和后端真实启动成功；
- `/health` 返回版本、Git、route availability、DeepSeek 配置、模型出口和 Docker 状态；
- 浏览器真实打开首页并确认需求入口、构建路线和 Local Lilies 状态；随后通过同一前端代理回读应用 API，确认权威数量为 11，并识别出首页首帧 `0` 是异步加载状态；
- 当前 OpenAPI 实数为 238 paths / 264 operations；
- 当前公开积木目录实数为 61；
- 当前 Builder operation 合同实数为 17；
- 聚焦测试：`78 passed, 2 failed`，未隐藏红灯。

### 9.2 两项当前红灯

| 红灯 | 当前观察 | 对功能结论的影响 |
| --- | --- | --- |
| 合同漂移测试 | 默认公开合同版本已改成 4，但测试又把运行中版本设置为 4 并期待出现 drift | 不能据此说合同漂移保护坏了；先要把测试改成真正不同的版本并复跑 |
| XLSX 工件路径测试 | runtime 把工件注册路径升级为 `.workflow-run-artifacts/{run_id}/artifacts/...`，测试仍期待 `artifacts/...` | XLSX 已实际生成且运行成功；当前红灯是路径合同预期未同步，但仍必须统一公开相对路径语义 |

本报告只诊断和记录，没有擅自修改这两项，因为用户本轮要求的是启动和报告，而不是授权修复当前 dirty worktree 的测试。

### 9.3 v0.4.13 阶段状态

| 范围 | 状态 |
| --- | --- |
| T01A 独立 Lilies core/daemon/CLI | 已完成 |
| T01B Builder 黑箱公开合同 | 已完成 |
| T01C 平台配对和 assignment bridge | 已完成 |
| T01D–G 正式协作、Studio、题包、资格测试 | 已完成，各自有声明上限 |
| T01K–L 同级独立 LiliesAgent 和 DevelopmentAssignment | 已完成，各自为 scoped controlled-local 证据 |
| T01H 六个 fresh-final 真实项目 | 进行中，r8 最终分母 0/6 |
| T01M Android 项目 | T01H 后执行，未完成 |
| T01N Windows 项目 | 未完成 |
| T01I 人格/视觉/可访问性 | 未完成 |
| T01J 总发布门与归档 | 未完成 |

组合台账里项目 2–6 的历史 status 为 passed，项目 1 为 needs_revision；但 r8 新合同要求六项全部从全新应用、环境和隔离 Builder 重跑，所以历史状态不能替代当前 fresh-final `0/6`。

## 10. 当前最重要的产品与架构问题

### 10.1 LiliesAgent 对平台的使用方式过重

平台已经提供增量 Draft API，但旧 Agent 框架仍维护大量本地阶段门和中间表示，模型需要在大 schema、阶段状态和工具序列化之间来回转换。它造成的主要问题是：

- 第一个大方案未整体通过时，不保存已经正确的小部分；
- 一个配置错误诱导模型重交整张图；
- 工具反馈虽然存在，但框架可能限制模型当轮只能继续调用同一个提交工具；
- 大 schema 重复进入上下文，Token 和时间被协议消耗，而不是业务推理消耗；
- 本地状态和平台 revision 可能产生两个真相源。

重构方向应该是让平台 Draft 成为唯一工作流真相，LiliesAgent 只保存会话、目标、当前错误和下一步，不再把工作流配置重新编译一遍。

### 10.2 公开手册质量不均匀

记录去重、匹配、人工输入、Connector、JSON/XLSX 手册已经非常具体；部分 Agent、模型和 RAG 手册仍使用通用模板反例。对较弱基座模型而言，模糊手册会显著提高试错次数。手册应继续补齐真实配置例、输入输出例、典型错误字段和最小可运行片段，但不应放入项目答案或最终图。

### 10.3 API 很全，但发现层还需要更轻

公开合同一次完整展开会非常大。现在已有 `block_search → block_get` 的渐进发现方式，这是正确方向。还应确保：

- 首次只提供任务需要的 5–12 个候选积木；
- 精确 schema 按需读取并缓存摘要；
- 错误只返回当前字段的 expected/actual；
- Builder 能根据成功 revision 继续，不必重复发送完整历史；
- 工具目录只暴露 assignment 真正允许的 Connector 操作。

### 10.4 实验 Harness 与普通产品路径必须继续解耦

隐藏 Seed、fresh-empty、角色暴露资格、归档和独立 verifier 是研究/验收机制，不是客户日常创建工作流的必需步骤。它们可以验证平台，但不能让普通 Builder 先理解整套实验制度才能增加一个节点。

### 10.5 “功能存在”与“真实交付成功”仍需分开

平台有训练、RAG、Connector、XLSX 和调度接口，但当前 r8 六项目尚未最终验收。最可信的产品报告必须同时列：

1. 平台提供了什么；
2. Builder 是否能发现和正确使用；
3. 工作流是否在公开 debug 数据上正确；
4. 同一不可变版本是否通过 held-out/hidden 业务输入；
5. 客户系统回读、工件和 receipt 是否一致。

## 11. 建议的下一步顺序

本报告不改变 Stage Contract。讨论结束并恢复任务时，仍按当前权威顺序继续 `T01H → T01M → T01N → T01I → T01J`。

在继续 T01H 前，最小且必要的工程动作应是：

1. 统一上面两项当前红灯的测试与公开合同，复跑同一 80 项聚焦测试；
2. 保持工作流是平台 Draft 单一真相，停止在 LiliesAgent 内重复“编译”整张图；
3. 用一次一个增量操作、一次一个可验证业务片段的方式恢复 Builder；
4. 继续记录每次成功/失败怎样影响 prompt、skill 和 Agent 框架，但经验必须通用，不能包含六项目答案；
5. T01H 每项仍以同一不可变版本的公开 debug + 三个受保护 Seed 为最终结果证据。

## 12. 证据来源

本报告主要依据：

- `docs/PRODUCT_NORTH_STAR.md`
- `PRELOAD_PROMPTS.md`
- `docs/evolution-control/PROGRAM_CHARTER.md`
- `docs/stage-reports/v0.4.13_lilies_local_agent_and_collaboration_pipeline.md`
- `docs/evolution-control/stage-contracts/v0.4.13-r8.json`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/blocks.py`
- `platform/backend/src/agent_platform/lilies_platform_contract.py`
- `platform/backend/src/agent_platform/lilies_platform_tools.py`
- `platform/backend/src/agent_platform/lilies_platform_api.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `platform/backend/src/agent_platform/typed_workbook.py`
- `platform/frontend/app/page.tsx`
- `platform/frontend/app/applications/[id]/page.tsx`
- `platform/frontend/app/runtime/[id]/page.tsx`
- `platform/frontend/app/governance/page.tsx`
- `platform/frontend/app/developer/collaboration/`
- 当前 `http://127.0.0.1:8001/openapi.json`、`/health` 和浏览器实机页面
- 当前聚焦测试命令：`.venv/bin/python -m pytest -q tests/test_v04_13_lilies_platform_client.py tests/test_v04_13_lilies_platform_api.py tests/test_record_pipeline_blocks.py tests/test_typed_workbook_artifact.py`

本报告没有读取平台数据库、隐藏 Seed 或 oracle，也没有为得到报告而改写既有应用、工作流或客户系统。
