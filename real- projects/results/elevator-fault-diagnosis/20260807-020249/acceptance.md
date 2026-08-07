# 验收单：电梯日常故障诊断（试题1 运营化）

- 时间：20260807-020249 (UTC)
- 应用：`6b422f03-dfae-42dd-9a25-fc47022bfeb1`
- 构建：needs_attention，异常：DeepSeek network error: 
- 架构审查：通过（节点：deployed_model_inference、end、model_turn、start、template_transform）

## 用例（0/2 通过）

### ❌ 故障过程样本（真实测试段）（运行：failed）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 is_fault | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 confidence | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 advice | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 model_version | 不通过 | 缺失（实际字段：[]） |
| is_fault = True | 不通过 | null |

### ❌ 正常过程样本（真实测试段）（运行：failed）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 is_fault | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 confidence | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 advice | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 model_version | 不通过 | 缺失（实际字段：[]） |
| is_fault = False | 不通过 | null |

## 结论：❌ 需要整改
