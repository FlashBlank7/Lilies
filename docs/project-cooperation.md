# 项目内多工作流协作

项目入口为 `/projects`，详情为 `/projects/{id}`。导入 ZIP 使用现有需求包格式，只创建空白项目；选择 Codex 也不会启动分析。需求包只读，Codex 在项目会话中分析和追问，提交完整需求文档。

当前客户入口是[项目进展与统一对话](project-conversation.md)：确认需求后，通过同一个对话搭建、试用、反馈及继续，不用手动选择内部阶段。下面的运行接口与高级操作继续保留在“开发详情”，供调试和兼容旧客户端。

## 四个实现增量

1. `project_store.py`、`projects.py`、`project_api.py`：项目容器和成员管理。主工作流仍是 Application，其 ID 同时是项目 ID；所有成员继续使用现有草稿、测试、版本和画布。一条成员只属于一个项目。主流程通过 `tool_name=workflow:<成员ID>` 调用成员，`input` 支持普通字段与 `$ref`。引用中的成员不能移除。
2. `workflow_runtime.py`、`project_blocks.py`：主流程驱动真实执行，支持顺序、条件、嵌套调用及共享记录。所有成员草稿在任务开始时通过一个数据库读取事务固定，调试无需发布。新增 `project_record` 读写积木，数据库服务按集合和记录键保存 JSON，并用修订号原子更新。
3. `project_agent_tools.py`：复用本机 Codex 接入，提供成员管理、指定工作流编辑与运行、共享记录读取、任务结果工具。`discuss` 沟通需求，`build` 建设与测试，`operate` 按需执行已建流程；业务阶段改图或程序会被拒绝。需求提交为全文替换，`requirements_submit(action="read")` 可先读取全文。
4. 前端 `/projects`：需求会话、成员列表和实际协作画布、业务记录、项目任务。保留“运行协作流程”和“交给统筹处理”两个入口。工具记录默认折叠，当前需求与业务结果优先展示。

## 运行与共享状态

任务请求示例：

```json
{"request_key":"request-001","mode":"workflow","inputs":{"application_id":"A"}}
```

POST `/api/v1/projects/{id}/tasks`。`mode=agent` 时还可提供 `message`，由项目 Codex 选择调用主流程或成员。相同 `request_key` 和内容返回原任务；键相同而内容不同返回 409。

任务保存原始输入、冻结草稿、主运行和成员运行、业务输出。`GET .../tasks/{task_id}` 返回关联运行的输入、输出、修订号、状态、错误及等待步骤。业务输入字段由项目自己的流程定义，示例字段不属于平台固定协议。

`project_record` 配置：

```json
{"action":"get","collection":"resources","key":"R1"}
```

不存在时返回 `found=false, revision=0, value=null`。写入：

```json
{"action":"put","collection":"resources","key":"R1","expected_revision":1,"value":{"owner":"A"}}
```

成功返回 `written=true` 和新修订号；竞争失败返回 `written=false, conflict=true` 和当前记录，不覆盖。工作流必须判断冲突，并自行定义重读、等待和业务幂等规则。单次记录更新是原子的；多个记录之间的业务一致性需要流程处理。重试同一运行节点的成功写入会复用第一次写入结果。

Owner API 使用 `GET/PUT .../records/{collection}/{key}`，PUT 冲突返回 409。读取集合使用 `GET .../records?collection=resources`，当前最多返回 1000 条。单条 JSON 对象上限 1 MB。没有跨项目记录节点，也不接受节点指定项目身份。

Codex 建设阶段可用 `workflow_run(action="start", wait=false)` 发起后立即返回，再用 `workflow_run(action="inspect", task_id="...")` 查询。连续发起两项异步任务后查询真实时间和结果，可测试并发；默认 `wait=true` 会等待结束。停止 Codex 同时取消其仍在运行的异步测试。

## 等待、停止和继续

- 工作流正常结束但业务需要续办时，末端输出 `task_status="waiting_input"`。单个已执行末端的字段直接作为结果；多个末端按节点 ID 分组。成员调用的结果位于 `output` 下。
- 人工输入积木暂停时，等待状态沿调用链传播，项目任务显示需要补充的步骤。`POST .../tasks/{task_id}/runs/{run_id}/input` 保存 `{"values":{...}}`，然后用户点击继续。
- `POST .../tasks/{task_id}/supplements` 保存 `message` 和补充 `inputs`；原始输入保留，下一次处理合并补充字段。此接口本身不执行。
- `POST .../tasks/{task_id}/stop` 取消任务及仍在运行的子运行。已完成结果和业务记录保留。
- `POST .../tasks/{task_id}/resume` 继续原任务，仍使用启动时固定的草稿。中断和失败的运行从持久化节点位置恢复；已经完成的节点保留。已正常结束但业务等待的流程重新检查条件，复用同任务中已成功完成且输入相同的成员结果。
- 重启将未结束的项目任务标为中断，不会自动运行。任务运行记录和业务记录保存在平台 SQLite 数据库中。

项目的测试工具运行真实工作流。保存的测试通过现有测试报告机制记录；每个用例在数据库中使用独立的空白记录空间，其父子工作流在该用例内共享记录并执行真实的修订号比较更新。测试不会读取或修改正式业务记录，不会继承其他用例或上一次测试的账本，也不需要轮换业务键才能重跑。初始记录应由用例中的工作流准备；空间由可信运行上下文绑定，节点不能指定。普通项目运行（包括手动联调和客户试用）仍使用项目共享业务记录。运行工具在 Docker 内看到 `/workspace`；企业包和确认文档只读。本期关闭网络、平台模型和外部连接器，只开放项目内文件及确定性运行工具。

保存的项目测试为每个用例准备独立文件副本，包括 `requirement-package/`、`requirements/`、`solution/`、`results/`。已有研究产物可作为测试输入；用例中新写的文件留在其测试目录，不覆盖项目原文件。需求包和确认文档的副本仍以只读方式挂载，主流程和成员使用同一用例目录。无需额外放置沙箱声明积木才能让项目脚本进入测试目录。运行输出和断言结果仍通过原测试报告查看。

macOS 上的测试文件副本优先使用 APFS 写时复制，避免每次回归重复占用整份工艺数据和已有结果的空间。副本拥有独立文件身份，修改内容或权限不会影响源文件或其他用例；文件系统不支持时仍使用普通复制。历史测试目录及结果不会自动清除。

## 验证与演示

独立输入包位于 `examples/project-cooperation/`，打包该目录的两个文件后即可导入。此包只包含业务诉求，没有工作流、代码、模型或答案，明确标记为平台测试。真实 Codex 演示与后端替身测试分开：替身验证 Agent 协议，业务协作测试通过真实工作流运行器。

```sh
MODEL_EGRESS_ENABLED=false .venv/bin/pytest -q tests/test_projects.py tests/test_project_agents.py tests/test_local_agents.py tests/test_codex_app_server.py
cd platform/frontend
npm run lint
npm test -- tests/projects.test.tsx tests/local-agent-session.test.tsx
```

保留现有单应用入口及独立运行方式。已有工业应用、需求文档和工作区不迁移。本期不接入 DeepSeek、Claude Code、自研 Agent、后台自动调度、跨项目复用或 MES。

### 2026-09-10 本机实际演示

项目：`9500f5f5-2ff2-45c0-9e1c-5ed2606a9c3b`（“平台测试：申请校验与资源分配”）。在运行中的应用打开 `/projects/9500f5f5-2ff2-45c0-9e1c-5ed2606a9c3b` 即可查看。本机 Codex 从独立需求包开始沟通、提交需求，再搭建主流程及两个成员；业务代码和测试由本次会话生成。

- 最终草稿：主流程 r51、申请校验 r63、资源分配 r107。实际画布测试 12/12 通过，覆盖校验拒绝、字段缺失、分配、内容冲突、资源等待及禁止绕过校验。报告在项目工作区 `results/canvas-tests-final.json`。
- 真实并发测试中，两个资源分配成员的运行时间重叠，仅一个占用 `TEST-PARALLEL2-R1`，另一个保留等待。生成的测试结果在 `results/assertions.json`；最终主流程已恢复为正式协作结构。
- 在业务记录页面人工释放 `TEST-20260910-R1`，然后在原等待任务 `109852c0-3d1e-441e-b22b-81f8671bd566` 点击“补充并继续”。同一任务成功分配给 `TEST-20260910-B`，记录修订号增至 4；保留旧申请和全部运行历史，已完成的校验成员复用。
- 在界面通过“交给统筹处理”发起 `ui-codex-demo-20260910-001`。本机 Codex 实际调用主流程与两个成员，再读取共享记录，确认 `UI-APP-001` 独占 `UI-R1`；任务 `2ddd5352-7a42-470a-a35f-36bd80ad80be` 为 `succeeded`，两条业务记录修订号均为 2，三条工作流版本未改变。
- 此业务任务曾因 Codex 恢复长会话时返回全量历史而连接失败。适配器现使用 CLI 的 `excludeTurns=true` 恢复现有线程；修复后通过界面继续的是原任务。自动回归覆盖超过 4 MB 的会话历史场景。

复现新的业务请求可使用以下输入，并指定新的业务请求标识。若同一资源已被占用，会保留等待，需在业务记录页面将资源更新为 `{"resource_id":"DEMO-R2","status":"free","owner_application_id":null}` 后继续原任务。

```json
{"application_id":"DEMO-APP-002","applicant":"演示用户","resource_id":"DEMO-R2"}
```

关联后端回归覆盖项目隔离、CAS 竞争、请求去重、冻结版本、停止子运行、重启后手动恢复、嵌套人工输入及原有单应用接口；前端 19 项测试和 TypeScript 检查通过。此次演示只验收平台协作能力，未启动或迁移既有工业项目。
