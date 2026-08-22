# 验收单：金蝶销售日报（真实星空·盲测）

- 时间：20260816-074246 (UTC)
- 应用：`5691e126-e08b-4da9-87e3-d88b9461297d`
- 构建：needs_attention，异常：builder stopped with invalid draft: workflow must contain exactly one start or schedule_trigger node; workflow must contain at least one end or answer node; at least one mandatory acceptance test is required
- 架构审查：通过（节点：）

## 用例（0/2 通过）

### ❌ 正常营业日 2018-12-24（真值：8客户38单合计621565.64，4达标）（运行：error: HTTP Error 422: Unprocessable Content）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 运行成功 | 不通过 | HTTP Error 422: Unprocessable Content |

### ❌ 无销售日 2018-12-23（全部记0未达标，日报照写）（运行：error: HTTP Error 422: Unprocessable Content）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 运行成功 | 不通过 | HTTP Error 422: Unprocessable Content |

## 结论：❌ 需要整改
