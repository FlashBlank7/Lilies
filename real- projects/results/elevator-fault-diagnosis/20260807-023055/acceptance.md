# 验收单：电梯日常故障诊断（试题1 运营化）

- 时间：20260807-023055 (UTC)
- 应用：`6b422f03-dfae-42dd-9a25-fc47022bfeb1`
- 构建：（复用现有工作流）
- 架构审查：通过（节点：deployed_model_inference、end、model_drift_monitor、model_turn、start、template_transform）

## 用例（0/2 通过）

### ❌ 故障过程样本（真实测试段）（运行：error: HTTP Error 422: Unprocessable Content）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 运行成功 | 不通过 | HTTP Error 422: Unprocessable Content |

### ❌ 正常过程样本（真实测试段）（运行：error: HTTP Error 422: Unprocessable Content）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 运行成功 | 不通过 | HTTP Error 422: Unprocessable Content |

## 结论：❌ 需要整改
