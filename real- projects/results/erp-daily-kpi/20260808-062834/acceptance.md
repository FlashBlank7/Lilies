# 验收单：ERP 门店日报达标（盲测）

- 时间：20260808-062834 (UTC)
- 应用：`694bac97-bdda-4ab8-a1ba-91728aaf259c`
- 构建：needs_attention，异常：builder stopped before mandatory tests passed
- 架构审查：通过（节点：end、http_request、iteration、llm、loop、record_collection_normalize、start、template_transform）

## 用例（0/2 通过）

### ❌ 正常营业日（真值可核）（运行：failed）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 summary | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 stores | 不通过 | 缺失（实际字段：[]） |
| summary 包含「华东一店」 | 不通过 | null |
| summary 包含「华东二店」 | 不通过 | null |
| summary 包含「西南一店」 | 不通过 | null |

### ❌ 闭店日（诚实零值）（运行：failed）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 summary | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 stores | 不通过 | 缺失（实际字段：[]） |
| summary 包含「没有销售数据」 | 不通过 | null |

## 结论：❌ 需要整改
