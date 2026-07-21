## 1. 按严重度排列的发现

### 严重：失败的反向契约可以被包装成整体通过并允许登记

`run_contracts()` 的总状态只检查 `positive_results`：

- 正向全部通过即把运行标为 `passed`；
- 反向用例即使为 `failed`，也不参与整体通过判断；
- 登记门禁依赖这个整体状态。

锚点：[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:5269)、[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:5312)。

这直接违反 T01C 的“禁止失败合同被提升为已验证”。现有测试只验证正向失败阻止登记，没有构造“正向通过、反向失败”的关键场景。

### 严重：没有为“每个生成操作”派生正向和反向契约

生成器只在请求 schema 存在 `required` 字段时创建反向用例；无必填输入的操作只有正向用例。

锚点：[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:5229)、[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:5929)。

三宿主证据也证明不是每个操作都有反向用例：

- 530 个生成操作；
- 530 个正向用例；
- 仅 451 个反向用例；
- 共 981 个用例。

锚点：[openapi_generalization_aggregate.json](/tmp/lilies-v0412-closure-review/evidence/openapi_generalization_aggregate.json)。

因此 T01C 的强制行为明确缺失，不只是测试覆盖不足。

### 高：T01E 所要求的交付性能字段没有完整报告

三个静态结果包有 parse/generate/end-to-end-generation 时间，但均缺少：

- `test_ms`；
- 端到端首次有效合同耗时，或明确的不可获得结果；
- 上游变更重验时间；
- 实际验证/修复尝试的计量。

`repair_attempts: 0` 是常量；`generation_attempts: 1` 也是固定写入。InvenTree 的 `attempts: 2` 实际是正向用例数量，不是修复尝试次数。

锚点：

- [paperless_openapi_generalization.json](/tmp/lilies-v0412-closure-review/evidence/paperless_openapi_generalization.json)
- [inventree_openapi_generalization.json](/tmp/lilies-v0412-closure-review/evidence/inventree_openapi_generalization.json)
- [chatwoot_openapi_generalization.json](/tmp/lilies-v0412-closure-review/evidence/chatwoot_openapi_generalization.json)
- [IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:6031)
- [IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:5333)

T01E 明文要求这些指标，不能把缺失字段降格成后续证据债务。

### 高：文档 URL 的网络边界存在 DNS/SSRF 缺口，且相应测试不完整

URL 检查只对 URL 中的主机文本尝试 `ip_address()`。域名解析到私网、loopback 或 link-local 地址时没有检查，存在 allowlisted 域名指向私网或 DNS rebinding 的风险。响应也是完整下载后才检查 5 MB，而不是流式限制传输。

锚点：[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:4513)、[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:4533)。

测试仅覆盖：

- loopback 字面 IP；
- 未真正请求成功的 URL；
- 内联超大字符串。

没有覆盖允许 URL 成功摄取、域名解析到私网、响应体超限、网络错误等。T01A 要求执行网络边界，当前证据不足且有实际安全缺口。

### 高：T01E 的“禁止专用协助扫描”不能证明其声明

扫描器：

- 只在当前 generalization runner 自身寻找三个构造器 token；
- 项目名分支扫描只覆盖 `platform/`；
- 只识别同时含有 `"--name"` 和 `"if args.name"` 的单一文本模式；
- 不扫描其他实验脚本、输入生成文件、sample inputs 或更一般的项目名/connector-id 分支。

锚点：[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:5940)。

diff 中没有发现实际 Paperless/InvenTree/Chatwoot 专用运行时代码，但现有扫描证据不足以支持“已完成禁止协助审计”的强声明。

### 中：T01B 的合同语义验证存在明显测试缺口

聚焦测试验证了 path、query、header、JSON body、Basic Auth 和简单响应 schema，但未验证合同列出的全部语义：

- cookie 参数；
- API key 与 Bearer；
- 非默认 content-type；
- 响应包络；
- 错误状态/错误 schema 语义；
- URL 编码和参数序列化边界。

锚点：[IMPLEMENTATION.diff](/tmp/lilies-v0412-closure-review/IMPLEMENTATION.diff:6810)。

代码有部分相应实现，不等于具备合同要求的确定性证据，因此 T01B 不能判 pass。

### 中：T01D/F 的构建与发布门禁只有实现者自述，没有归档原始结果

允许审查的 JSON 证据中没有：

- 前端 typecheck/lint/build 结果包；
- focused/full regression 结果包；
- Ruff 结果包；
- evolution-control 结果；
- 这些结果的归档摘要/digest。

[STAGE_REPORT.md](/tmp/lilies-v0412-closure-review/STAGE_REPORT.md) 声称 `786 passed, 85 xfailed`、lint/build/Ruff/演进控制通过，但按审查规则不能把实现者总结当证据。

浏览器 JSON 确实支持导入、审查、通过登记、失败阻断和人类可读 expected/actual；但浏览器路径仅使用读操作，没有补足发布门禁的其他原始证据。

锚点：[browser-evidence.json](/tmp/lilies-v0412-closure-review/evidence/browser-evidence.json)。

### 低：总分母没有被删除，但不能据此宣布泛化实验通过

聚合结果诚实保留：

- 981 总用例；
- 3 passed；
- 1 failed；
- 977 not_run；
- Paperless、Chatwoot 分别标为环境受限；
- InvenTree 写合同失败。

这部分没有发现总分母删除或虚构三宿主通过。但“静态生成完成”和“强制实验验收通过”是两回事。

锚点：[openapi_generalization_aggregate.json](/tmp/lilies-v0412-closure-review/evidence/openapi_generalization_aggregate.json)、[inventree_live_contract.json](/tmp/lilies-v0412-closure-review/evidence/inventree_live_contract.json)。

版本规模为 22 个文件、6414 行新增，涵盖摄取、生成、运行、契约、Studio 和实验；没有“版本规模不足”问题，但规模不能抵消上述验收失败。

## 2. 强制任务结果

| 任务 | 结果 | 主要原因 |
|---|---|---|
| V04-12-T01A | **FAIL** | DNS 解析后的私网边界未执行；URL 摄取及网络/响应超限测试不完整。 |
| V04-12-T01B | **FAIL** | 缺少 cookie、API key/Bearer、content-type、错误语义及响应包络等强制语义的确定性测试证据。 |
| V04-12-T01C | **FAIL** | 并非每个操作都有反向用例；反向失败可被整体包装为 passed 并可能登记。 |
| V04-12-T01D | **FAIL** | 浏览器主路径和失败可理解性有证据，但 typecheck/lint/production build 只有自述，没有允许范围内的原始归档证据。 |
| V04-12-T01E | **FAIL** | 总分母诚实，但缺首次有效合同/测试/上游重验等逐宿主指标；禁止专用协助扫描过弱；真实结果为 3/981 通过、1 失败、977 未运行。 |
| V04-12-T01F | **FAIL** | A–E 尚有强制失败；完整回归、Ruff、前端门禁、演进控制及结果 digest 没有原始证据，发布门禁不能闭合。 |

## 3. Closure Audit 总结论

**FAIL。v0.4.12 不得闭环或归档。**

存在阻塞性强制行为缺失和失败包装为通过的问题。尤其是反向契约失败不影响整体 `passed`，足以单独否决 Closure Audit。

没有发现删除总分母、把 InvenTree 写失败改写为通过，或把两个环境受限宿主伪造成真实通过；但 STAGE_REPORT 将 T01A–E 标为“已完成”属于越过现有证据的虚假完成状态。

## 4. 允许的声明上限

当前最多可以声明：

> v0.4.12 实现了一个 OpenAPI 3.0/3.1 到 Connector 的候选原型，并在受控 mock/browser 路径验证了部分生成、执行、失败展示和登记阻断能力。对三份冻结规范静态发现 955 个操作，生成 530 个，记录 425 个不支持操作；共生成 981 个契约用例，其中仅真实执行 4 个：3 个通过、1 个失败，977 个未运行。Paperless 与 Chatwoot 只有冻结规范静态生成证据；InvenTree 只有读合同通过、写合同失败证据。

不得声明：

- v0.4.12 Closure Audit 或发布门禁通过；
- 自动契约验证闭环可靠；
- 每个生成操作都有正反契约；
- 三宿主 REST/OpenAPI 泛化已验证通过；
- 955 个操作或 530 个生成操作均已运行验证；
- 安全文档 URL 摄取边界已经完整验证；
- 客户生产可用；
- 非 REST 协议可用；
- 替换流程性能优于原流程。