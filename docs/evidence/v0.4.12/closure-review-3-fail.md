# v0.4.12 第三次独立闭环审查

- 审查方式：新的只读上下文根据锁定 Stage Contract、当前差异和机器证据反向检查。
- 结论：`FAIL`，仅 `V04-12-T01F` / `V0412-CLOSE-03` 未通过。
- 版本规模门禁：通过。

## 已通过部分

- `T01A / CLOSE-01`：DNS 解析地址经过私网检查并固定到实际连接，流式 5 MB 边界与网络失败有确定性测试。
- `T01B`：生成 manifest、通用 REST 语义、认证、参数、内容类型、错误状态和完整响应包络有确定性覆盖。
- `T01C`：每个生成操作都有正反用例，任一失败会使运行失败并阻止登记，源漂移会使证据失效。
- `T01D`：浏览器证据覆盖导入、审查、通过登记、失败阻断、截图摘要、布局、控制台和网络。
- `T01E / CLOSE-02`：最新 InvenTree 读写运行 `4/4` 通过，POST 身份和独立 GET 副作用回读一致；旧失败保留在 `5 passed / 1 failed / 1054 not_run` 唯一分母及 8 条执行记录中。
- `CLOSE-04`：合成 wrapped/unwrapped 原始响应、长度和摘要均持久化，确定性测试分别验证通过与失败。

## 唯一阻塞

门禁虽然从结果记录重算唯一分母，但没有把旧运行的顶层汇总和聚合 retained-failure 指针双向绑定。只读篡改探针发现，下列任一修改后 `live_contract_gate()` 仍错误返回 `passed`：

1. 旧运行 `contract_run.status: failed -> passed`；
2. 旧运行 `contract_run.failed: 1 -> 0`；
3. 聚合 `retained_failure.case_id -> fabricated.positive`。

因此门禁还不能支持“历史失败误报会阻止发布”的声明。

## 必需修复

- 根据旧运行的 result records 重算并核对 `status/passed/failed/skipped/unsupported/blocked_by_environment`。
- 将聚合 retained-failure 的 artifact、case ID、operation ID、actual 和数量绑定到真实失败记录。
- 加入上述三类负向篡改测试并重跑发布门禁。
