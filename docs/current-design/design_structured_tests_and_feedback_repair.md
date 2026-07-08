# design_structured_tests_and_feedback_repair

状态：已完成  
对应 plan：`docs/workingon/plan_apply_lilies_design_notes_2026_07_08.md`  
完成日期：2026-07-08  
设计性质：下一阶段实现设计，不直接修改后端代码

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

非目标：

- 不在第一版替换现有 `WorkflowTestCase`。
- 不把所有业务质量判断都交给 LLM-as-judge。
- 不允许反馈修复绕过 `content_hash/tested_hash` 发布门禁。
- 不把“测试框架可读性”做成纯前端展示；后端必须保存结构化报告。

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
| `severity` | `blocking` / `warning` / `informational`。 |
| `evidence_policy` | 需要结构断言、工具证据、URL 引用还是人工审阅。 |
| `owner` | Builder、人工测试者或模板维护者。 |

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
- `raw_workflow_test_result`
- `linked_nodes`
- `linked_tools`
- `requires_retest`

### `FeedbackPatchSpec`

人类反馈转成可审计 patch。

字段：

- `target_node_id`
- `target_field`
- `operation`: `replace` / `increase` / `decrease` / `add_constraint` / `remove_constraint`
- `reason`
- `source_test_frame`
- `requires_retest`

### `RepairPlanSpec`

当多个测试框架失败时，不能让 Builder 随意修。需要先生成局部修复计划。

| 字段 | 含义 |
| --- | --- |
| `repair_id` | 修复计划 ID。 |
| `source_report_ids` | 触发修复的测试框架报告。 |
| `repair_level` | `node_tuning` / `edge_repair` / `module_rebuild` / `full_rebuild`。 |
| `target_nodes` | 受影响节点。 |
| `target_edges` | 受影响边。 |
| `patches` | 一个或多个 `FeedbackPatchSpec`。 |
| `expected_retests` | 修复后必须重跑的测试框架。 |
| `risk` | 修复可能影响哪些下游模块。 |

## 4. 对象关系

```text
WorkflowTestCase
  -> TestFrameSpec
  -> TestFrameReport
  -> RepairPlanSpec
  -> FeedbackPatchSpec[]
  -> Draft mutation
  -> content_hash changes
  -> retest
```

关系约束：

- `WorkflowTestCase` 继续作为底层可执行测试。
- `TestFrameSpec` 负责解释测试意图、风险和人类可读 rubric。
- `TestFrameReport` 负责保存本次运行证据，不只存在前端。
- `RepairPlanSpec` 负责把一个或多个失败报告归并成修复策略。
- `FeedbackPatchSpec` 只能通过 draft mutation 生效，不能直接改 published version。

## 5. 小说生成测试示例

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

## 6. 修复机制

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

### 6.1 局部修复流程

```text
test_run
  -> frame reports
  -> failed frame grouping
  -> repair scope proposal
  -> human approval or guarded auto
  -> draft patch
  -> content_hash invalidates tested_hash
  -> retest affected frames
  -> full mandatory suite before publish
```

局部修复规则：

- 单个 frame 失败且 `linked_nodes` 明确时，优先 `node_tuning`。
- 失败涉及数据缺失或引用错误时，优先 `edge_repair`。
- 同一 module 内多个 frame 同时失败时，进入 `module_rebuild`。
- plan 或需求理解错误时，回到 `BuildPlanSpec`，不做局部 patch。

### 6.2 反馈输入类型

| 反馈类型 | 示例 | 转换方式 |
| --- | --- | --- |
| 结构反馈 | “缺少检索节点”。 | 增加 required node 或模块重建。 |
| 内容反馈 | “小说设定不够严格”。 | 修改 prompt/template constraints。 |
| 工具反馈 | “没有真实引用 URL”。 | 修改 tool routing 或 required tool evidence。 |
| 风险反馈 | “这个工具不能自动执行”。 | 更新 passmode 或 permission gate。 |

## 7. Meta-test

测试本身也要被检查，否则 Builder 可能生成“看起来有测试、实际没约束”的测试套件。

Meta-test 检查项：

| 检查项 | 失败含义 |
| --- | --- |
| 每个核心需求至少有一个 `TestFrameSpec`。 | 验收覆盖不足。 |
| mandatory test 至少包含结构断言。 | 容易被黑箱输出绕过。 |
| 有外部工具的 workflow 必须有工具证据要求。 | 无法证明真实工具调用。 |
| 高风险工具必须关联 passmode 或 permission policy。 | 工具治理缺失。 |
| 每个 failed frame 必须给出 repair scope 或人工判断入口。 | 失败不可操作。 |

Meta-test 第一版可以作为 `draft_validate` warning；进入发布门禁前再决定哪些升级为 error。

## 8. 数据流与存储

```text
WorkflowStorage
  stores TestSuite / WorkflowTestCase
WorkflowRuntime.run_test_suite()
  produces raw test result
TestFrameReporter
  maps raw result -> TestFrameReport[]
WorkflowStorage.mark_tested()
  stores tested_hash + raw report + frame report
DraftPatchService
  applies FeedbackPatchSpec
WorkflowStorage.save_draft()
  changes content_hash and clears tested_hash
```

存储要求：

- `validation_report_json` 增加 `frames` 字段，保持向后兼容。
- frame report 必须包含 raw result 引用，避免可读报告与真实结果分叉。
- patch history 记录 `source_test_frame`，方便追踪“为什么改这个节点”。
- retest report 必须能比较修复前后同一个 frame 的结果。

## 9. UI/API 方向

画布测试面板应展示：

1. 测试框架名。
2. 风险目标。
3. 本次结果一句话。
4. 证据链接。
5. 失败字段。
6. 建议修复范围。
7. “应用局部修复”按钮。

后端 API 可先保持兼容，在 `validation_report_json` 中增加 `frames` 字段。

建议 API 演进：

| API | 用途 |
| --- | --- |
| `GET /applications/{id}/draft/tests/frames` | 查看测试框架和最近报告。 |
| `POST /applications/{id}/draft/tests/run` | 返回 raw report + frame report。 |
| `POST /applications/{id}/draft/repairs/plan` | 从失败报告生成修复计划。 |
| `POST /applications/{id}/draft/repairs/apply` | 应用经批准的 patch。 |

第一版可以不新增 endpoint，只在现有测试和 draft edit API 上增加内部对象；但设计应按上述边界保留扩展口。

## 10. Agent 节点策略

不直接删除 `claude_agent`，但新建工作流默认不应使用它。

强制方式：

- Builder prompt 继续要求显式 agent architecture bricks。
- mandatory tests 必须包含 `required_node_types`。
- 如果 workflow 只有 `claude_agent`，测试框架应标记为“不可审计架构风险”。
- 迁移工具把 `claude_agent` 展开为显式 bricks。

## 11. 实现步骤

### Step 1：Frame report 只读化

- 保留 `WorkflowTestCase`。
- 增加 `TestFrameSpec` 和 `TestFrameReport`。
- `run_test_suite()` 后生成 `frames`。

完成标准：

- 测试面板能显示测试意图、风险目标、结果摘要和证据。
- raw JSON 仍可查看。

### Step 2：Meta-test warning

- `draft_validate` 或测试前检查 frame 覆盖。
- 缺少 required node/tool evidence 时给 warning。
- 高风险缺失逐步升级为 blocking。

完成标准：

- 测试不足能被明确指出，而不是等运行后才发现。

### Step 3：Repair plan

- 根据 failed frames 生成 `RepairPlanSpec`。
- 修复范围限制到 node/edge/module。
- 人类确认后应用 patch。

完成标准：

- 小修不会触发全图重建。
- patch 后 `tested_hash` 清空。

### Step 4：Retest 与回归

- 先重跑 affected frames。
- 发布前仍跑 mandatory suite。
- 报告对比修复前后结果。

完成标准：

- 用户能看到“修了什么、为什么修、修后哪些测试变绿”。

## 12. 风险与约束

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 可读报告和 raw result 不一致 | 测试结论不可信。 | frame report 必须引用 raw result。 |
| LLM-as-judge 误判 | 内容质量判断漂移。 | 第一版以结构/工具证据/人工 review 为主。 |
| 局部修复破坏下游 | 修一个节点导致别的测试失败。 | affected frames 后必须跑 mandatory suite。 |
| repair scope 错误 | Builder 修错地方。 | human approval 或 guarded auto 分级。 |
| 测试框架过重 | 维护成本增加。 | 先只包装 mandatory tests，不强制覆盖所有辅助测试。 |

## 13. 实验切片

对应实验：

- E03：结构化测试可读性。
- E04：节点级修复 vs 全图重建。
- E08：移除默认 Agent 节点。
- E11：自然语言 Draft Patch。

第一批实验设计：

| 实验 | 最小样例 | 指标 |
| --- | --- | --- |
| E03 | 同一测试报告的 raw JSON vs frame report。 | 人工判读时间、误判率、字段覆盖。 |
| E04 | 一个局部 prompt 失败、一个 edge 错误。 | 修复成功率、成本、回归失败数。 |
| E08 | `claude_agent` wrapper vs 显式 architecture blocks。 | 可审计性、required node 覆盖、失败可定位性。 |

## 14. 验收标准

- 测试人员不看原始 JSON 也能判断测试意图和结果。
- 一个失败测试能给出修复范围，而不是只提示 failed。
- 人类反馈能形成 `FeedbackPatchSpec`。
- 局部修复后 `content_hash` 改变，`tested_hash` 失效，并触发重测。
- frame report 能追溯到 raw `WorkflowTestCase` 结果。
- meta-test 能指出测试覆盖不足。
- E03/E04 实验完成并生成 `.docx` 报告。

## 15. 完成证据

本设计已补齐：

- 对象关系。
- 数据流与存储。
- 局部修复流程。
- meta-test。
- API 方向。
- 实现步骤。
- 风险与实验切片。
- 可执行验收标准。

因此本文件可以作为下一阶段实现测试可读化和反馈驱动局部修复的设计依据。

## 16. 引用资产

- `docs/intellectual-assets/asset_blockflow_language_system.md`
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
