# design_structured_tests_and_feedback_repair

## 1. 问题

当前测试已经有强约束能力：`required_node_types`、`required_tool_nodes`、`required_tools`、`minimum_tool_calls`、`require_cited_tool_urls` 能防止黑箱 Agent 节点冒充可审计架构。

但测试结果对测试人员不友好：

- 输出偏 JSON 字段。
- 看不出一个测试属于什么测试框架。
- 人类反馈很难定位到应该修哪个节点。
- 对“还不错”的工作流，整体重建成本高、上下文压力大、效果不稳定。

## 2. 设计目标

- 让每个测试属于一个可读测试框架。
- 测试结果能被人类快速判断。
- 人类反馈可以转成局部修复，而不是全图重建。
- 建立 meta-test：测试本身也被验证是否覆盖结构、工具证据和业务验收。

## 3. 新对象草案

### `TestFrameSpec`

包装现有 `WorkflowTestCase`，增加可读语义。

| 字段 | 含义 |
| --- | --- |
| `frame_id` | 测试框架 ID。 |
| `frame_name` | 例如“大纲与设定遵循度测试”。 |
| `risk_target` | 这个测试防止什么失败。 |
| `rubric` | 人类可读评分标准。 |
| `linked_requirements` | 对应需求。 |
| `workflow_test_case_id` | 底层 `WorkflowTestCase`。 |
| `repair_hint` | 失败后优先修复的节点类型或参数。 |

### `TestFrameReport`

把运行结果转成人类可读报告。

字段：

- `frame_name`
- `result_summary`
- `passed`
- `evidence`
- `failed_assertions`
- `human_review_prompt`
- `suggested_repair_scope`

### `FeedbackPatchSpec`

人类反馈转成可审计 patch。

字段：

- `target_node_id`
- `target_field`
- `operation`: `replace` / `increase` / `decrease` / `add_constraint` / `remove_constraint`
- `reason`
- `source_test_frame`
- `requires_retest`

## 4. 小说生成测试示例

测试框架：

```text
frame_name: 大纲与设定遵循度测试
risk_target: 生成文本偏离用户设定或故事大纲
rubric:
  - 是否保留主角身份
  - 是否保留时代/世界观
  - 是否覆盖大纲关键事件
  - 是否没有引入冲突设定
repair_hint:
  - 优先检查 prompt/template_transform 节点
  - 再检查 context_builder 节点
```

底层仍使用 `WorkflowTestCase`：

- `required_node_types`: `["template_transform", "model_turn", "answer"]`
- `assertions`: 至少检查输出存在、长度、结构。
- 内容遵循度可以先走 human review，再逐步引入 LLM-as-judge。

## 5. 修复机制

### 修复层级

| 层级 | 触发条件 | 动作 |
| --- | --- | --- |
| Node tuning | 单个测试框架失败，结构基本正确。 | 修改 prompt、temperature、strength、budget、tool setting。 |
| Edge repair | 数据没有传到正确节点。 | 修边或引用路径。 |
| Module rebuild | 模块级测试整体失败。 | 重建模块，不重建全图。 |
| Full rebuild | plan 错误或验收目标理解错。 | 回到 BuildPlanSpec。 |

### 节点强度

“强度”不应是一个泛字段，而应映射到不同节点的可调参数：

| 节点类型 | 强度含义 |
| --- | --- |
| `model_turn` | system 指令强度、temperature、输出格式约束。 |
| `template_transform` | 模板约束密度、必须包含字段。 |
| `budget_gate` | 预算阈值。 |
| `round_limit` | 最大轮数。 |
| `tool_executor` | 工具调用许可、重试次数、超时。 |

## 6. UI/API 方向

画布测试面板应展示：

1. 测试框架名。
2. 风险目标。
3. 本次结果一句话。
4. 证据链接。
5. 失败字段。
6. 建议修复范围。
7. “应用局部修复”按钮。

后端 API 可先保持兼容，在 `validation_report_json` 中增加 `frames` 字段。

## 7. Agent 节点策略

不直接删除 `claude_agent`，但新建工作流默认不应使用它。

强制方式：

- Builder prompt 继续要求显式 agent architecture bricks。
- mandatory tests 必须包含 `required_node_types`。
- 如果 workflow 只有 `claude_agent`，测试框架应标记为“不可审计架构风险”。
- 迁移工具把 `claude_agent` 展开为显式 bricks。

## 8. 验收标准

- 测试人员不看原始 JSON 也能判断测试意图和结果。
- 一个失败测试能给出修复范围，而不是只提示 failed。
- 人类反馈能形成 `FeedbackPatchSpec`。
- 局部修复后 `content_hash` 改变，`tested_hash` 失效，并触发重测。
- E03/E04 实验完成并生成 `.docx` 报告。

## 9. 引用资产

- `docs/intellectual-assets/asset_blockflow_language_system.md`
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
