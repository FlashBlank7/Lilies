# design_plan_first_and_modular_builder

状态：已完成  
对应 plan：`docs/workingon/plan_apply_lilies_design_notes_2026_07_08.md`  
完成日期：2026-07-08  
设计性质：下一阶段实现设计，不直接修改后端代码

## 1. 问题

当前 `WorkflowBuilder` 的优势是安全：它通过受控 draft tools 一次添加一个节点或一条边，避免一次性生成大 JSON。但复杂需求里，Builder 容易被局部操作牵着走，难以保持全局结构。

用户观察是正确的：好的 plan 像一个工作流，它先规定模型接下来怎样思考、怎样拆解、怎样回答。Lilies 应该把 plan-first 变成 Builder Team 创建 `BlockFlow` 的显式阶段。

## 2. 设计目标

- Builder 先生成可审阅的 `BuildPlanSpec`，再搭 `BlockFlow`。
- 复杂任务先拆成 reusable modules，每个 module 可以单独搭建、测试、封装。
- 最终由 assembly workflow 组装模块，而不是让一个 Builder 在单一画布里硬搭到底。
- 保留当前单步 draft mutation 的可追踪性。
- 为后续 Builder-as-workflow 做铺垫。

非目标：

- 不在第一版移除现有 `WorkflowBuilder` coordinator loop。
- 不允许 Builder 直接提交完整 `WorkflowSpec` 绕过 draft tools。
- 不把 `BuildPlanSpec` 当成可执行工作流；它是构建计划，不是 `WorkflowSpec`。
- 不在第一版实现自动模板市场评分，只预留复用策略字段。

## 3. 核心对象

### `BuildPlanSpec`

建议新增一个计划对象，不直接等同于 `WorkflowSpec`。

字段草案：

| 字段 | 含义 |
| --- | --- |
| `goal` | 用户需求的可交付目标。 |
| `acceptance_criteria` | 验收条件。 |
| `modules` | 模块计划列表。 |
| `global_constraints` | 预算、权限、工具、深度、模型等约束。 |
| `test_strategy` | 测试分层和测试框架。 |
| `reuse_strategy` | 是否检索模板、复用已有模块。 |
| `risk_profile` | 工具、预算、外部调用、人工确认等风险。 |
| `plan_status` | `draft` / `reviewed` / `approved` / `rejected` / `superseded`。 |
| `source_refs` | 需求、模板、历史报告或智力资产引用。 |

### `ModulePlan`

字段草案：

| 字段 | 含义 |
| --- | --- |
| `module_id` | 模块稳定 ID。 |
| `purpose` | 模块解决的问题。 |
| `inputs` / `outputs` | 模块边界。 |
| `candidate_templates` | 可复用模板。 |
| `expected_blocks` | 预期 block 类型。 |
| `tests` | 模块级测试。 |
| `reuse_depth` | 允许嵌套复用的深度。 |
| `build_status` | `planned` / `building` / `tested` / `failed` / `ready`。 |
| `failure_policy` | 失败时重试、局部修复、降级或请求人工。 |

### `AssemblyPlan`

复杂任务需要明确模块如何组装，避免每个模块都正确但整体不可用。

| 字段 | 含义 |
| --- | --- |
| `assembly_goal` | 最终 `BlockFlow` 的整体目标。 |
| `module_bindings` | 模块输出到模块输入的绑定关系。 |
| `global_tests` | 跨模块测试和端到端测试。 |
| `published_modules` | 使用已发布模块还是当前 build 内部模块。 |
| `fallback_path` | 模块不可用时的降级方案。 |

## 4. Builder pipeline

建议流程：

1. `requirement_intake`：读取需求、当前 draft、catalog、template suggestions。
2. `plan_create`：生成 `BuildPlanSpec`。
3. `plan_review`：人类或自动 gate 检查 plan。
4. `module_build`：对每个 `ModulePlan` 创建局部 `BlockFlow`。
5. `module_test`：模块级 tests 先通过。
6. `module_publish`：可复用模块进入 template/module store。
7. `assembly_build`：组装模块为最终 `BlockFlow`。
8. `suite_test`：全局测试通过。
9. `publish`：发布版本或进入 `ready`。

### 4.1 控制流

```text
BuildRequest
  -> requirement_intake
  -> plan_create
  -> plan_review
  -> module_build[*]
  -> module_test[*]
  -> assembly_build
  -> suite_test
  -> ready / published / needs_attention
```

控制流要求：

- `plan_create` 只产生计划对象，不修改 draft。
- `plan_review` 通过后，Builder 才能进入 draft mutation。
- `module_build` 内部仍使用现有受控工具：`draft_add_node`、`draft_connect`、`test_add`、`draft_validate`、`test_run`。
- `assembly_build` 只组装已测试模块或已发布模板，不重新解释原始需求。
- 任意阶段失败都要写入 Build events，并能定位到 `plan`、`module`、`assembly` 或 `suite`。

### 4.2 数据流

```text
User requirement
  -> BuildPlanSpec
  -> ModulePlan[]
  -> module Draft / module BlockFlow
  -> module test report
  -> AssemblyPlan
  -> final Draft / final BlockFlow
  -> suite test report
  -> ApplicationVersion or ready Draft
```

关键不变量：

- `WorkflowSpec` 的 `content_hash` 仍由 draft 内容决定。
- 修改 `BuildPlanSpec` 不应自动改变 draft 的 `content_hash`，除非它触发 draft mutation。
- 每个 `ModulePlan` 必须能追溯到对应节点、子图或模板展开记录。
- 最终发布仍必须满足 `tested_hash == content_hash`。

### 4.3 状态模型

建议 Build 增加细分状态或阶段字段：

| 阶段 | 允许进入条件 | 退出条件 |
| --- | --- | --- |
| `planning` | Build 创建后。 | `BuildPlanSpec` 生成成功。 |
| `plan_review` | plan 可读且结构完整。 | 自动 gate 或人工确认。 |
| `module_building` | plan approved。 | 所有必需 module 生成完成或失败。 |
| `module_testing` | module draft 已生成。 | 模块级测试通过或进入 repair。 |
| `assembly_building` | 必需 module ready。 | assembly draft 生成完成。 |
| `suite_testing` | final draft valid。 | mandatory tests 通过或失败。 |
| `ready` / `published` / `needs_attention` | 与现有 Builder 状态兼容。 | 由现有发布和失败逻辑决定。 |

## 5. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `workflow_models.py` | 增加 `BuildPlanSpec`、`ModulePlan`、`DraftPatchPlan`。 |
| `builder.py` | 增加 plan-first 阶段和 plan tools；保留单步 mutation。 |
| `template_store.py` | 支持 module 级模板检索。 |
| `applications.py` | 可选增加 batch draft operation，但默认仍按单步审计落库。 |
| `workflow_storage.py` | Build 状态保存 plan、module 状态和 plan revision。 |
| `workflow_runtime.py` | 支持 module-as-workflow 或 workflow-as-tool 的调用证据。 |

### 5.1 第一版最小改动

第一版不需要大改 API。推荐最小落地：

1. 在 `workflow_models.py` 增加纯数据模型：`BuildPlanSpec`、`ModulePlan`、`AssemblyPlan`。
2. 在 `builder.py` 的 Build state 中增加 `build_plan` 和 `plan_events`。
3. 新增 builder tool：`plan_create`、`plan_update`、`plan_mark_reviewed`。
4. `module_build` 阶段仍调用现有 draft tools，不新增批量写入。
5. `test_run` 报告中附加 `module_id`，让失败能回指 `ModulePlan`。
6. `workflow_storage.py` 只保存 plan JSON 到 build/team state，不改变发布模型。

### 5.2 第二版扩展

第二版再考虑：

- module-level draft 或 hidden application。
- module publish 到 TemplateStore。
- assembly workflow 显示模块节点。
- module-as-tool 调用证据。
- plan diff 和 plan versioning。

## 6. 关键决策

### 保留单步 mutation

不建议让 Builder 直接提交完整 `WorkflowSpec`。完整图可以先作为 plan，但落库时仍拆成可审计操作。

### 模块优先，而不是更大的 prompt

复杂度扩展不靠更长上下文，而靠：

- 模块边界。
- 模块级测试。
- 模块可复用。
- Assembly 层只关心模块 I/O。

### 复用深度要有硬限制

建议初始默认：

- `reuse_depth = 1`：只展开一层模板。
- `max_module_count = 6`
- `max_nodes_per_module = 12`
- `max_assembly_nodes = 20`

后续用实验 E06 调整。

### Plan 不是万能审批

plan-first 不能替代测试。`BuildPlanSpec` 只证明 Builder 的理解可审阅，不证明产物正确。最终可靠性仍来自：

- `draft_validate`
- mandatory tests
- `tested_hash == content_hash`
- Platform Harness 边界

### 模块不是隐藏黑箱

模块化复用不能把不可审计的 `claude_agent` 包起来当模块。模块至少要暴露：

- I/O contract
- internal node type summary
- test report
- tool usage summary
- harness requirements

## 7. 实现步骤

### Step 1：只读 plan 生成

- Builder 在任何 draft mutation 前生成 `BuildPlanSpec`。
- UI/API 可读取 plan。
- plan 失败时 Build 进入 `needs_attention`，不创建半成品 draft。

完成标准：

- 简单需求能生成 1 个 module。
- 复杂需求能生成多个 module 和 assembly plan。
- plan 能列出 required node types 和测试策略。

### Step 2：plan-reviewed mutation

- 只有 plan reviewed 后才允许 `module_build`。
- 每次 draft mutation 记录 `module_id`。
- 测试失败能回指 module。

完成标准：

- Build events 中能看到 plan -> module -> node mutation 的链路。
- 失败定位不再只有全局 error。

### Step 3：module test 与局部 repair

- 每个 module 有最小测试。
- module 失败优先局部 repair。
- 多次 repair 失败再回到 plan。

完成标准：

- 同一需求中，单个模块失败不会直接触发全图重建。
- repair report 能说明修复范围。

### Step 4：assembly 与 template reuse

- 已测试 module 可进入 assembly。
- assembly 只做模块 I/O 绑定和全局测试。
- 可选把高质量 module 写入 TemplateStore。

完成标准：

- 最终 `BlockFlow` 可追溯到 plan、modules 和 tests。
- 发布门禁保持不变。

## 8. 风险与约束

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| plan 写得漂亮但执行失败 | 形成虚假的可控感。 | plan 必须绑定 tests 和 mutation evidence。 |
| 模块过多导致组合成本上升 | Builder 成本和运行成本增加。 | 初始限制 `max_module_count` 和 depth。 |
| module 变成新黑箱 | 可审计性下降。 | 强制模块暴露 I/O、节点摘要和测试证据。 |
| plan review 增加用户负担 | 使用体验变重。 | 简单任务自动 review，复杂任务才人工确认。 |
| 模板复用引入旧错误 | 旧模板污染新工作流。 | 复用前检查最近测试时间和质量分。 |

## 9. 实验切片

对应实验：

- E01：Plan-first Builder
- E02：单节点增量 vs 模块化增量
- E06：模块化工作流复用深度

第一批实验只需要验证设计方向，不需要完整产品化：

| 实验 | 最小样例 | 关键比较 |
| --- | --- | --- |
| E01 | 一个复杂内容生成工作流、一个工具调用工作流。 | direct incremental vs plan-first incremental。 |
| E02 | 8、16、32 节点复杂度阶梯。 | 单节点增量是否更易断裂。 |
| E06 | 模板嵌套 depth 0/1/2。 | 可读性、测试通过率、定位失败成本。 |

## 10. 验收标准

- 对同一复杂需求，plan-first Builder 至少能输出可读 `BuildPlanSpec`。
- 模块计划能映射到具体 `required_node_types` 和测试框架。
- 生成失败时能指出失败发生在 plan、module build、module test 还是 assembly。
- 模块化构建不绕过现有 draft tools 和发布门禁。
- 设计明确区分 `BuildPlanSpec`、`ModulePlan`、`WorkflowSpec` 和 `BlockFlow`。
- plan-first 与 direct incremental 的对照实验有 `.docx` 报告。

## 11. 完成证据

本设计已补齐：

- 非目标。
- 核心对象和状态字段。
- 控制流与数据流。
- 最小实现步骤与第二版扩展。
- 风险与约束。
- 实验切片。
- 可执行验收标准。

因此本文件可以作为下一阶段实现 `BuildPlanSpec` 和 plan-first Builder 的设计依据。

## 12. 引用资产

- `docs/intellectual-assets/asset_blockflow_language_system.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`
