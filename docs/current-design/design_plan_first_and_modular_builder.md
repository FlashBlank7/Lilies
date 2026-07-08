# design_plan_first_and_modular_builder

## 1. 问题

当前 `WorkflowBuilder` 的优势是安全：它通过受控 draft tools 一次添加一个节点或一条边，避免一次性生成大 JSON。但复杂需求里，Builder 容易被局部操作牵着走，难以保持全局结构。

用户观察是正确的：好的 plan 像一个工作流，它先规定模型接下来怎样思考、怎样拆解、怎样回答。Lilies 应该把 plan-first 变成 Builder Team 创建 `BlockFlow` 的显式阶段。

## 2. 设计目标

- Builder 先生成可审阅的 `BuildPlanSpec`，再搭 `BlockFlow`。
- 复杂任务先拆成 reusable modules，每个 module 可以单独搭建、测试、封装。
- 最终由 assembly workflow 组装模块，而不是让一个 Builder 在单一画布里硬搭到底。
- 保留当前单步 draft mutation 的可追踪性。
- 为后续 Builder-as-workflow 做铺垫。

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

## 5. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `workflow_models.py` | 增加 `BuildPlanSpec`、`ModulePlan`、`DraftPatchPlan`。 |
| `builder.py` | 增加 plan-first 阶段和 plan tools；保留单步 mutation。 |
| `template_store.py` | 支持 module 级模板检索。 |
| `applications.py` | 可选增加 batch draft operation，但默认仍按单步审计落库。 |
| `workflow_storage.py` | Build 状态保存 plan、module 状态和 plan revision。 |
| `workflow_runtime.py` | 支持 module-as-workflow 或 workflow-as-tool 的调用证据。 |

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

## 7. 验收标准

- 对同一复杂需求，plan-first Builder 至少能输出可读 `BuildPlanSpec`。
- 模块计划能映射到具体 `required_node_types` 和测试框架。
- 生成失败时能指出失败发生在 plan、module build、module test 还是 assembly。
- plan-first 与 direct incremental 的对照实验有 `.docx` 报告。

## 8. 引用资产

- `docs/intellectual-assets/asset_blockflow_language_system.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`
