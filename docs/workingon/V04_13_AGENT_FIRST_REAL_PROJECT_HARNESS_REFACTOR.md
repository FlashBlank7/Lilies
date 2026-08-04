# v0.4.13 智能体优先的真实项目测试基础设施重构债务

日期：2026-08-02

来源：EXP-LILIES-001 十分钟快速复跑

状态：已记录，待当前强制任务完成后按用户优先级进入正式设计；本文件不选择
下一阶段任务，也不改变当前 Stage Contract。

## 一、问题结论

当前真实项目测试基础设施可以完成项目，但对 Builder 智能体过于笨重。环境、
故障代理、凭据、Connector tenant、幂等账本、Seed、运行和验收分别由不同状态
与命令管理。人类维护者能靠上下文记住它们，智能体则必须同时推理多个隐形
生命周期，容易把基础设施问题误判成工作流、权限或平台能力问题。

这不是“智能体不够聪明”可以完全解决的问题。Prompt 和 Skill 可以降低错误率，
但基础设施本身应让正确顺序成为默认行为，让半启动和跨世代污染无法静默发生。

## 二、本次确认的具体阻挡

### 1. `up` 不代表环境真的可用

`environment_control up` 启动 Paperless、InvenTree、数据库和缓存，但不启动
工作流实际访问的故障代理与 attestation 服务。Docker 全绿时，18010、18011、
18002 仍可能不可用。Builder 看到的是“All connection attempts failed”，却无法
从 `up` 的成功结果得知环境只启动了一半。

### 2. reset 没有形成统一的新环境世代

客户数据库重建后，InvenTree 内部主键改变；平台侧 Connector tenant 和幂等
账本仍可能沿用上一代。相同业务幂等键随后绑定到新的请求体，产生
`connector idempotency key is bound to different input`。

正确安全语义不是删除幂等保护，而是让环境重建自动创建新的
`environment_generation_id`，并让 Connector tenant、binding、secret 和幂等
账本统一属于该世代。

### 3. fault state、Seed 回执和宿主快照会跨 reset 残留

Docker volume 已清空并不代表 state root 已清空。`fault-state.json`、
`seed-receipts-*.json` 和 `host-snapshot-*.json` 仍可能描述上一轮环境。智能体要
人工判断哪些文件是当前证据、哪些只是历史文件，增加了误读和污染风险。

### 4. 底层错误被业务分类掩盖

项目一工作流将非临时写入错误统一落入“权限拒绝”结果，导致幂等冲突表现为
12 条 permission denied。Builder 首先看到业务分类而不是稳定的平台错误码，
很容易修错层。

平台与工作流应保留两层字段：

- `business_decision`：权限拒绝、冲突、重复、人工停写等业务结论；
- `technical_failure`：连接失败、认证失败、幂等冲突、schema 错误、宿主 403 等
  稳定技术分类与原始回执摘要。

未知技术错误不得自动冒充已知业务结论。

### 5. Builder 被迫理解过多实验内部概念

当前路径要求 Builder 同时理解 compose project、state root、package revision、
fault proxy、attestation、Seed receipt、host snapshot、binding tenant、secret owner、
environment generation、Connector ledger 和 verifier 归因。多数概念不属于客户
业务，也不应成为搭建工作流的前置知识。

## 三、重构目标

目标不是增加更多脚本或治理门，而是提供一个轻量、公开、可观察的“真实项目
运行环境会话”。Builder 只需要任务说明、工作目录和平台公开 API。

一个环境会话应拥有唯一身份：

```text
project_id + environment_generation_id
├─ customer services
├─ boundary services / proxies
├─ scoped credentials
├─ connector tenant + binding
├─ idempotency ledger generation
├─ fault profile
├─ seed receipt
└─ host snapshot lineage
```

任何子状态不是当前 generation 时，平台应明确拒绝运行并返回可操作错误，而不是
允许半启动或跨世代复用。

## 四、最小重构方案

### A. 统一环境生命周期

提供一个公开环境会话 API 或等价最小入口：

```text
prepare(project, seed)
  -> generation_id
  -> 启动客户服务和全部边界服务
  -> 初始化并 seed
  -> 创建/轮换 scoped credentials
  -> 创建对应 Connector tenant/binding/ledger generation
  -> 运行全部实际入口健康检查
  -> 返回一份有界公开 receipt

stop(generation_id)
  -> 停止该 generation 的容器和边界进程
  -> 保留数据卷或证据的恢复信息
```

这不是强制“一键完成所有测试”，而是保证一次环境准备的语义完整。

### B. 环境 receipt 只暴露 Builder 真正需要的信息

```json
{
  "project_id": "EXP-LILIES-001",
  "environment_generation_id": "...",
  "status": "ready",
  "customer_endpoints": ["paperless", "inventree"],
  "boundary_endpoints": ["paperless_proxy", "inventree_proxy", "attestation"],
  "connector_tenants": ["..."],
  "secret_refs": ["secret://.../inventree-builder"],
  "health": "passed",
  "started_at": "...",
  "evidence_scope": "public"
}
```

Builder 不应读取 token 值、数据库、隐藏输入或宿主内部映射。

### C. reset 必须原子推进 generation

reset 成功的提交点必须同时完成：

1. 客户数据代际切换；
2. fault state 代际切换；
3. Seed 与 snapshot lineage 切换；
4. scoped credential 切换；
5. Connector tenant/binding/ledger generation 切换；
6. 边界服务健康。

任一步失败都返回 `environment_prepare_failed` 和具体阶段，不得留下表面 ready 的
半状态。

### D. 统一稳定错误分类

平台公开回执至少区分：

- `host_unreachable`
- `authentication_failed`
- `authorization_denied`
- `idempotency_input_conflict`
- `response_schema_mismatch`
- `transient_host_error`
- `business_conflict`
- `unknown_technical_failure`

工作流可以把这些技术结果映射为业务决定，但不得丢失原分类。

### E. 证据按 generation 自动归档

Seed receipt、fault log、run、Connector receipt、artifact、host snapshot 和 verifier
结果都绑定同一 generation。历史文件仍可保留，但默认查询只返回当前 generation，
无需 Builder 手工移动或改名旧文件。

## 五、重构后的 Builder 心智模型

Builder 只需要理解四层：

1. 业务：什么时候允许写，什么时候停。
2. 平台：用哪些公开积木和 Connector operation 表达业务。
3. 环境：当前 generation 是否 ready。
4. 证据：业务结果、副作用和工件是否一致。

compose、代理进程、数据库主键、幂等账本存储、Seed 文件位置和 verifier 内部归因
都不应泄漏成 Builder 的日常操作负担。

## 六、验收标准

未来重构至少应满足：

1. 从 stopped 到 ready 只产生一个 generation receipt；ready 时所有实际入口可用。
2. 连续 reset 两次不会复用 Connector ledger generation，也不会发生旧请求污染。
3. 旧 fault state、Seed receipt 和 snapshot 不会被默认当成当前证据。
4. 故意关闭边界代理时，prepare 明确失败，不能返回 ready。
5. 幂等冲突保留为 `idempotency_input_conflict`，不会被归类为权限拒绝。
6. Builder 无需读取源码、数据库或隐藏材料即可完成环境准备、工作流运行和证据读取。
7. EXP-LILIES-001 公开 debug 可由 Builder 在十分钟内完成，且无需人工管理多个
   后台进程或移动历史状态文件。
8. 资源停止后没有孤儿容器、代理或 attestation 进程。

## 七、范围控制

这项重构属于通用开发/测试基础设施，不是项目一专用业务补丁。不得把项目一的
采购字段、固定 Connector operation、Seed 值、oracle 或最终工作流写进通用层。

当前先继续既定项目任务。正式实施前需要把本债务纳入后续有效 Stage Contract
或由用户明确提升优先级，避免它再次扩张成阻塞真实业务交付的大型框架工程。

## 八、EXP-LILIES-002 新增债务：公开 workspace capability

项目二 115 秒快跑新增三项通用证据：

1. `/tests/run` 收到边界外绝对 workspace 时返回裸 500，而普通 `/runs` 对不存在的
   相对 workspace 返回 422。相同概念在两个入口上的错误合同不一致。
2. Builder 只能传原始文件路径，却无法通过公开合同确定平台实际 workspace root；
   在自己的临时根目录创建同名相对路径，平台仍不可见。
3. 构建工具在应用已经创建后才于测试阶段失败，但调用者没有先得到 durable
   application receipt，后续流水线丢失 application_id 并继续制造噪音错误。

后续最小重构应增加：

- 由平台创建并返回不暴露宿主绝对路径的 `workspace_handle`；测试、草稿运行、
  发布版运行和工件读取统一接受该 handle；
- 所有 workspace 边界错误统一返回可操作的 4xx 稳定错误码，不能裸 500；
- 长操作在第一个持久资源创建后立即返回或持久记录 resource receipt，使智能体
  能从失败阶段继续；
- 官方 Builder 客户端默认 fail-fast，并把后续步骤标记 skipped，不使用空资源
  身份继续请求。

重构验收新增：同一个 workspace handle 可被强制测试、Draft run、published run
和 artifact API 使用；错误路径不要求 Builder 知道平台 state root 或本机目录布局。

## 九、EXP-LILIES-003 新增债务：发布图与事件运行绑定分裂

项目三 259 秒快跑证明业务能力本身已经可用：随机化验收 8/8，真实 Home Assistant
事件也能驱动发布版完成持久计时与恰好一次通知。但发布 version 1 后，事件订阅列表
仍然为空。Builder 必须发现这个空状态，再调用另一套 API，手工重复填写：

- WebSocket 地址和允许主机；
- greeting、认证、订阅与确认报文；
- Secret 引用；
- 事件匹配、事件身份和输入字段映射；
- 静态业务输入、workspace 和重连策略。

这对智能体的阻挡很严重。工作流图看起来已经拥有“事件订阅触发器”，但发布语义只
保存了一个名字，并没有产生可运行的入口；同时网络绑定、状态机、Secret、workspace
和客户环境又分属不同生命周期。Builder 必须理解平台内部拼装方式，而不是只理解
客户业务。

后续把它列为高优先级重构债务：

1. 触发器节点应携带可发布的声明式 subscription manifest，或引用一个平台管理的
   event-source handle；发布版本时原子创建并绑定运行入口。
2. 若缺少运行绑定，publish receipt 和应用页面必须明确返回
   `missing_runtime_binding` 及唯一下一步，不能让订阅列表悄悄为空。
3. 事件订阅必须绑定不可变 application version，而不是只绑定会变化的 application。
4. Secret、workspace、网络允许列表和客户环境 generation 应以 capability handle
   引用，不能要求 Builder 重述宿主路径和握手细节。
5. 一个 deployment receipt 应统一显示 graph version、event source、worker、客户
   endpoint、workspace、secret refs 和资源状态；stop 应按 receipt 一次释放全部资源。

重构验收新增：发布带事件触发器的工作流后，无需第二次手工翻译字段映射即可看到
一个 version-bound 的 ready subscription；若环境不完整，发布回执给出稳定、可执行
的单一错误。Builder 的日常心智模型只保留“业务事件 → 输入合同 → 发布部署”，不再
暴露 WebSocket 握手和多套生命周期拼接。

## 十、EXP-LILIES-004 新增债务：能力存在但部署合同没有整体恢复

项目四 8 分钟快跑中，模型、生产图、公开业务、提升、回滚和发布都完成。但再次运行
暴露四项通用智能体阻力：

1. OpenAPI 生成请求明确包含 `getLatestTimeseries` 和 `saveAlarm`，首个 generation
   却只生成读操作；注册前合同运行也因实际列表只含读操作而显示通过。Builder 必须
   另外比较 requested/generated，理解复杂 discriminator，并手写通用 schema overlay。
2. 同一 ThingsBoard 接口仅因测试端点换成故障代理，就被表示成 Connector version 2。
   接口合同版本与部署 profile/generation 混在一起，增加 manifest、binding、policy、
   Secret 和应用许可的拼接负担。
3. 模型生命周期验收使用固定设备名。`environment ensure` 复用已有 ThingsBoard volume
   后，v2 已经提升成功，脚本却因同名设备存在而中途失败，不能从 durable receipt 继续。
4. 安全 503 重试能力已经存在，但当前共享平台进程没有随项目环境加载受信
   pre-dispatch attestation。图配置了三次重试、代理也返回证明头，运行仍在第一次 503
   安全停止。能力“代码存在”不等于本次 deployment ready。

后续高优先级重构：

- Connector generation receipt 必须把 requested/generated 差集作为显著的 partial
  状态；若未明确确认 gap，不得用缺失操作的合同运行晋升为完整 ready。
- 提供针对不支持 OpenAPI schema 的安全 overlay suggestion，不要求 Builder 读取实现
  或重建整个版本。
- Connector interface version、deployment profile 和 environment generation 分离；
  客户接口不变时，故障代理只是另一个受控 profile，不应复制 manifest 版本。
- `prepare(project)` 返回一个统一 deployment receipt，原子确认 host、connector、
  bindings、policies、secrets、model deployment、workspace 和 retry attestations ready。
- 所有客户夹具、模型部署和测试身份默认由 environment generation 命名；脚本重入时
  要么复用同一 durable receipt，要么创建新身份，不使用全局固定名。
- 503 验收前平台应公开返回 `pre_dispatch_retry_ready=true/false`；false 时在运行前失败，
  不能等到随机 Seed 第 11 条才暴露。

重构验收新增：同一个项目在不删除 volume、不重启整台平台的情况下连续运行两次，
Builder 只拿新的 generation receipt 即可完成；requested Connector 操作无静默缺失；
503 安全重试 readiness 在第一条业务运行前可见且与实际运行一致。

## 十一、EXP-LILIES-005 新增债务：Program deployment 不是一个可恢复整体

项目五 9 分 22 秒快跑暴露的不是新业务能力缺口，而是五套部署状态没有统一：

1. Program profile 只能在平台启动时从文件加载。项目一启动的共享平台返回空列表，
   为了使用已有 Program 能力必须重启整个平台进程。
2. 平台数据目录不变，但从标准输入启动临时进程后，隔离正则的 multiprocessing spawn
   无法恢复 `<stdin>` 主模块；相同工作流因此统一报 `regex_execution_failed`。
3. `agent-platform-sandbox:latest` 标签存在，开发检查便认为 ready，但镜像实际比当前
   Dockerfile 旧，缺少 Dockerfile 已声明的 Node。Program 到运行时才报 `node not found`。
4. 经验构建器通过 144 次增量 mutation 恢复图。节点替换会删边，旧 edge idempotency
   key 却仍被占用；最终还漏掉 start→validate→gate 入口链。它不是期望状态构建器。
5. Seed runner 在创建 Actual 测试账户失败后吞掉 CLI stderr，仅返回通用错误，也没有
   独立 environment receipt。Builder 无法在十分钟内安全决定是否重试。

后续统一重构：

- Program profile 改为平台可治理、可热加载的版本化 capability resource；profile
  receipt 包含 profile digest、runner image digest、runtime (`node@24.15.0`)、工具包
  integrity、Secret refs、网络和缓存探针。
- 平台所有受支持入口必须通过同一 launcher contract，并在启动自检中实际跑一次
  multiprocessing、Program read probe 和 artifact write probe；禁止仅检查端口 ready。
- 镜像 readiness 比较内容 digest 和声明能力，不按 mutable tag 是否存在判断。
- 提供应用 desired-state apply：一次请求比较节点、边、测试和 metadata，原子收敛；
  reconcile key 绑定完整目标摘要，不绑定历史逐边操作。
- 项目环境 prepare 先完成 Actual 登录、预算发现、独立账户/数据创建和新鲜客户端回读，
  再签发 generation receipt；失败保留脱敏但可操作的错误类、阶段和恢复动作。
- 测试 runner 不得把环境准备失败计为 Seed 业务失败，也不得隐藏到无法区分参数、缓存、
  权限、网络和宿主状态。

重构验收新增：同一平台无需重启即可激活版本化 Program profile；两次连续项目五快跑
不重建镜像、不复用旧账户、不发生 idempotency key 冲突；`prepare` 成功后公开测试与
Seed 不会再出现 runtime/tool/cache 缺失，失败回执能直接指向唯一恢复步骤。

## 十二、EXP-LILIES-006 新增债务：客户项目不是一个可恢复的部署单元

项目六在 4 分 49 秒内完成 3/3 公开测试和随机 Seed 16/16，说明预测、约束计划、人工
治理、ERP 草稿和幂等重放能力已经存在。实际阻挡来自项目启动方式：

1. 旧 ERPNext volume 和容器都在，但当前 compose 要求的管理员密码变量没有保存在旧
   create-site 容器。Builder 看到的是“环境存在但无法按现合同启动”。
2. 为恢复同一个客户环境，需要分别理解 Docker compose、Frappe site、管理员密码、
   API key/secret、平台 Secret 和 Connector binding；这些本应是一个环境世代的状态。
3. 公开题包 manifest 正确指向 `fixtures/public-inputs/...`，但既有快速脚本和操作经验
   使用旧的 `public-inputs/...`。路径错误发生在模型准备前，却没有由统一材料句柄失败关闭。
4. application、Connector generation、模型 deployment、workspace 和客户环境均已成功
   创建，但流水线没有一个统一 receipt。若调用者不是 fail-fast 并主动保存各 ID，就会
   因后续路径错误丢弃已有资源并重新初始化。

后续整体重构应提供项目级 `prepare/deploy/stop` 协议，而不是更多项目积木：

- `prepare` 接收公开需求包和授权范围，返回一个 `project_deployment_handle`，其中只暴露
  客户可理解的 host、Connector、model、Secret refs、workspace、generation 和 readiness；
- 环境凭据轮换、site 恢复、Connector binding 与 secret 更新由 handle 的 generation
  原子完成，Builder 不接触 compose 插值变量和内部 site 命令；
- 公开材料通过内容寻址 handle 读取。文件移动时 manifest 仍解析到同一内容；不存在时
  在任何模型或工作流操作前返回稳定 4xx 和唯一修复动作；
- 每个成功创建的资源立即进入 durable receipt。失败后恢复使用同一 handle，不重建应用、
  模型、Connector 或客户环境；
- `stop(project_deployment_handle)` 一次停止该项目全部容器、代理、订阅和 worker，但保留
  应用、发布版本和只读证据；平台 UI 明确显示哪些资源仍在消耗 CPU/内存。

重构验收新增：连续两次从同一 ERPNext volume 复跑项目六时，Builder 只需需求目录、
平台地址和 deployment handle；无需读取或设置 Docker/Frappe 内部变量，公开材料路径
变动不破坏内容引用，失败后可从同一 application receipt 继续，停止后资源面板显示零
项目容器运行。
