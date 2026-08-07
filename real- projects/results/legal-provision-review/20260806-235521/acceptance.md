# 验收单：法律条文审查应用（试题4 原味需求）

- 时间：20260806-235521 (UTC)
- 应用：`42a05263-349d-4566-a004-ba83471fce29`
- 构建：published
- 架构审查：通过（节点：end、knowledge_index_sync、knowledge_retrieval、model_turn、question_classifier、start、template_transform）

## 用例（0/3 通过）

### ❌ 条文审查：试用期违法条款（运行：failed）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 findings | 不通过 | 缺失（实际字段：[]） |
| findings 包含「试用期」 | 不通过 | null |
| findings 包含「第十九条」 | 不通过 | null |
| findings 包含「第二十条」 | 不通过 | null |

### ❌ 法律问答：两年合同试用期上限（运行：failed）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 answer | 不通过 | 缺失（实际字段：[]） |
| 输出包含字段 citations | 不通过 | 缺失（实际字段：[]） |
| answer 包含「个月」 | 不通过 | null |
| citations 包含「第十九条」 | 不通过 | null |

### ❌ 条文推荐：违约金纠纷文本（运行：failed）

| 检查项 | 结果 | 实际 |
| --- | --- | --- |
| 输出包含字段 recommendations | 不通过 | 缺失（实际字段：[]） |
| recommendations 包含「第五百八十五条」 | 不通过 | null |

## 结论：❌ 需要整改
