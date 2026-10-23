# 方案与调试

工作流页的「方案与调试」把分析、拆分、独立调试和联调放在一个可操作入口。画布仍用于修改节点，任何阶段都可保存草稿。

## 操作路径

1. 展开「问题与能力边界」，填写目标、完成条件、平台与外部服务分工、待明确事项。
2. 在画布或通过项目助手创建成员工作流，声明输入、输出及 HTTP 服务引用。左侧显示每条流程及其调用关系。
3. 在「模拟服务」选择记录预设，或保存 Python 实现；填写初始状态及故障场景。此时不访问真实服务。
4. 选择成员流程。编辑合成输入、上传 JSON/CSV 样例，或选择历史运行的实际输出。绑定服务与场景，保存样例并运行。下游无需等待上游运行。
5. 查看本次结果、成员实际输入输出、HTTP 请求和模拟状态。「使用此输出调试下游」固定一份输出副本及来源，不随历史数据变化。
6. 在「串联验证」选择流程顺序，以字段选择器把上游输出填入下游输入，保存为普通主流程；选择模拟服务后运行整条链。
7. 失败时「继续原任务」沿用原快照与服务状态；「修改后修复」创建新任务并保留原状态及操作身份；「重置并重跑」创建全新测试状态。历史任务结果不被修复任务改写。
8. 模拟范围验证后，负责人在项目设置配置真实连接，再显式选择绑定运行。模拟通过不等同真实接入完成。

## 模拟服务包 v1

项目 API：`GET /projects/{id}/simulators/presets`、`GET/POST /projects/{id}/simulators`、`PUT /projects/{id}/simulators/{service_id}`（均在 `/api/v1` 下）。保存采用 `expected_revision`；服务只能被本项目引用。

字段：`contract_version: 1`、`name`、`description`（接口说明与假设）、`implementation: records|python`、`code`、`allowed_paths`、`allowed_methods`、`idempotency_header`、`initial_state`、`scenarios`。

- `allowed_paths` 支持精确路径或 `/**` 前缀范围。示例 `/items/**`。
- `scenarios` 是名称到状态覆盖字段的映射，覆盖为顶层替换。每次新会话从该版本的初始状态构造；恢复不重新初始化。
- `records` 预设提供 `GET /records?page=1`、`GET /records/{id}`、`POST /update`（`id, changes`）、`POST /delete`（`id`）。状态包含 `records`、`page_size` 和可选 `failures: [{path, method, remaining, status, after_write}]`。幂等头仅支持 `Idempotency-Key`；留空表示不声明幂等支持。
- 代码上限 200 KB；每次沙箱调用超时 20 秒、响应和状态合计最多 5 MB。不提供长期后台进程。stdout 留给协议输出，说明应写入响应或 stderr。

自写实现示例：

```python
def handle(request, state, context):
    # method, path, query, headers（小写键）, body
    if request['method'] == 'GET' and request['path'] == '/items':
        return {'response': {'status': 200, 'headers': {},
                             'body': {'items': state.get('items', [])}},
                'state': state}
    return {'response': {'status': 404, 'body': {'error': 'unknown path'}},
            'state': state}
```

`context` 包含固定的 `now`、`seed`、`scenario`。返回 HTTP `response`、完整新 `state`，以及可选 `fault: "disconnect"`：平台先保存新状态再模拟响应丢失。写入重试仍由 HTTP 积木根据幂等声明或结果查询判断。声明幂等的自写实现必须真正保存请求身份并防止重复作用，平台不会替实现伪造幂等性。

自写代码只在现有 Docker 禁网沙箱运行，独立目录只放实现代码；工作流代码也使用独立模拟目录。不会获得真实凭据或项目生产文件。内部 HTTP transport 提供模拟网关，仍经过 HTTP 积木、请求检查、重试及恢复；它不是另起一个 TCP 服务，也不证明外部 DNS、TLS 或原厂 API 兼容性。

## 运行与版本

HTTP 节点可用 `service_ref: "media"` 和相对 `path`，运行绑定为 `{"media": {"service_id": "…", "scenario": "partial"}}`。已有 `connection_id` 作为别名显式映射即可模拟。禁止绝对 URL 和隐含真实连接回退。

真实任务通过 `service_bindings: {"media": "项目连接 ID"}` 绑定；指定旧连接标识时也可显式重绑。真实连接的凭据注入、路径/方法范围、禁用与版本检查沿用原机制。

样例在现有草稿测试用例中保存（`debug` 元数据记录来源和绑定）；运行固定原样例与修订。模拟包、会话状态及终态服务快照存于 SQLite，恢复跨进程保留状态。工作流、模拟包或样例修改后，旧任务显示「旧配置」。结束节点检查失败时任务为未完成，不把旧报告当本次成功。

首版模拟可运行 start/end、HTTP、禁网代码、模板/变量/条件/循环/迭代及项目内成员流程调用。其他尚未隔离的副作用节点给出具体不支持说明；草稿仍可保存。混合正式节点的模拟流程不会调用真实节点替代。

## 共享助手接口

- `block_catalog`：查积木，搭建流程。
- `project_workflows / workflow_draft`：编辑本项目草稿。
- `project_services(presets/list/inspect/save)`：读取接口、保存实现与场景。
- `project_debug(cases/save_case/run/inspect/resume/chain)`：保存样例、独立/整链运行、读结果与恢复。
- `project_calls`：正式调用人已加入的项目能力。

空配置表示没有目标。两种助手通过相同工具执行实际项目任务；草稿模拟不会自动授予正式调用权限。

## 本地复现

使用 README 的独立调试环境启动方式，保持模型出站关闭。准备现有工作流 Docker 沙箱后：

```bash
PYTHONPATH=platform/backend/src:. .venv/bin/python scripts/seed_workflow_debug_demo.py \
  --api http://127.0.0.1:18541 --web http://127.0.0.1:18542
PYTHONPATH=platform/backend/src:. SIMULATION_DOCKER=1 \
  .venv/bin/pytest tests/test_project_debug_media.py -q
```

种子脚本通过普通账号、项目助手工具 API 创建四条草稿、模拟服务与样例，不调用模型。演示账号信息仅存 Git 忽略的 `.local-debug/workflow-demo.json`，不写入报告。`completed` 场景是人工合成的完成状态，用于独立验证核对流程，不是上游实际运行的证明。正式链路另用实际输出验证。

报告：[影音项目演练](reports/workflow-debug-20261023/report.md)。
