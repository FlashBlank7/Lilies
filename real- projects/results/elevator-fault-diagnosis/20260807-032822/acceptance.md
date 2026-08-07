# 验收单：电梯日常故障诊断（试题1 运营化）

- 时间：20260807-032822 (UTC)
- 应用：`6b422f03-dfae-42dd-9a25-fc47022bfeb1`
- 构建：（复用现有工作流）
- 架构审查：不通过，缺少 deployed_model_inference（节点：end、llm、start、template_transform）

## 用例（2/2 通过）

### ✅ 故障过程样本（真实测试段）（运行：succeeded）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 is_fault | 通过 | 存在 |
| 输出包含字段 confidence | 通过 | 存在 |
| 输出包含字段 advice | 通过 | 存在 |
| 输出包含字段 model_version | 通过 | 存在 |
| is_fault = True | 通过 | true |

### ✅ 正常过程样本（真实测试段）（运行：succeeded）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 is_fault | 通过 | 存在 |
| 输出包含字段 confidence | 通过 | 存在 |
| 输出包含字段 advice | 通过 | 存在 |
| 输出包含字段 model_version | 通过 | 存在 |
| is_fault = False | 通过 | false |

## 结论：❌ 需要整改
