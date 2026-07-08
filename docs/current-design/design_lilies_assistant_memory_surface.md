# design_lilies_assistant_memory_surface

状态：已完成  
对应 plan：`docs/workingon/plan_apply_lilies_design_notes_2026_07_08.md`  
完成日期：2026-07-08  
设计性质：长期方向设计与下一阶段实验设计，不直接修改后端代码

## 1. 问题

Lilies 如果只像一个平台，就容易被理解为“画布 + 插件 + 一个 Builder”。用户真正想要的是类似 Codex/Claude Code 的使用体验：她能理解项目、记住长期上下文、调取文件和模板、帮用户继续几天前的工作。

这个方向很有吸引力，但风险也大：活动监视、文件系统封装和长期记忆都必须有明确权限和审计边界。

## 2. 设计目标

- 把 Lilies 升级成可调用的项目助手，而不只是后台平台。
- 让助手能检索项目记忆、模板、实验报告和阶段文档。
- 对文件系统访问做显式封装和权限控制。
- 用小模型承担低风险任务：翻译、分类、检索、路由、摘要。
- 根据任务难度选择 operator、workflow depth 和模型。

非目标：

- 不默认后台监视用户全部活动。
- 不让助手裸读写任意文件系统路径。
- 不让小模型绕过 task monitor boundary。
- 不把长期记忆做成不可审计的黑箱向量库。
- 不在第一版替换 Studio 的核心画布操作。

## 3. 核心对象

### `ActivityMemoryRecord`

| 字段 | 含义 |
| --- | --- |
| `memory_id` | 记忆记录 ID。 |
| `workspace_id` | 工作区或项目 ID。 |
| `source_kind` | `file_change` / `command` / `test_result` / `report` / `stage_event` / `chat_summary`。 |
| `summary` | 可读摘要。 |
| `source_refs` | 文件、commit、报告、测试输出引用。 |
| `sensitivity` | `public` / `project_internal` / `secret_risk`。 |
| `retention_until` | 保留期限。 |
| `created_at` | 记录时间。 |
| `created_by` | 用户、系统或助手。 |

### `AssistantContextPack`

助手每次回答不应读取所有记忆，而应生成受控上下文包。

| 字段 | 含义 |
| --- | --- |
| `request_id` | 本次助手请求。 |
| `selected_memories` | 被选中的记忆记录。 |
| `selected_templates` | 被选中的模板。 |
| `selected_docs` | 被选中的 stage/design/asset 文档。 |
| `permission_scope` | 本次可读写范围。 |
| `task_profile` | 难度与风险画像。 |
| `excluded_sources` | 因权限或敏感性被排除的来源。 |

### `DraftPatchPlan`

自然语言修改画布时生成的 patch 计划。

| 字段 | 含义 |
| --- | --- |
| `patch_id` | patch ID。 |
| `target_application_id` | 目标应用。 |
| `target_revision` | 目标 draft revision。 |
| `user_intent` | 用户修改意图。 |
| `affected_nodes` | 受影响节点。 |
| `affected_edges` | 受影响边。 |
| `operations` | add/update/delete/connect/disconnect。 |
| `expected_retests` | 应重跑的测试。 |
| `preview` | 人类可读预览。 |

## 4. 产品面能力

### Activity memory

记录用户授权范围内的项目活动：

- 最近修改的文件。
- 最近运行的命令。
- 最近生成的报告。
- 最近失败的测试。
- 当前 stage 的 task plan。

不默认“天天监视”。必须有：

- 开关。
- 范围。
- 保留时间。
- 查看和删除入口。
- 敏感路径排除。

### 文件系统代理

不要让助手裸操作文件系统。建议通过 `FileSystemProxy`：

- 只暴露工作区白名单。
- 写入前生成 patch 或 operation preview。
- 记录审计事件。
- 高风险操作进入 `approval_required` passmode。

文件系统代理必须遵守：

- 读路径白名单。
- 写操作 preview。
- 删除、移动、大规模格式化必须人工确认。
- 密钥、env、凭证文件默认排除，除非用户明确授权。
- 每次写入都生成 audit event。

### 自然语言修改工作流

用户在画布上说：

> 把这个小说生成流程加强设定遵循度，并把测试输出改成可读报告。

系统应生成 `DraftPatchPlan`，而不是重建整个 `WorkflowSpec`。

执行流程：

```text
natural language edit
  -> AssistantContextPack
  -> DraftPatchPlan
  -> preview
  -> apply patch
  -> content_hash changes
  -> affected tests
  -> mandatory suite before publish
```

## 5. 难度路由

建议引入 `TaskDifficultyProfile`：

| 字段 | 含义 |
| --- | --- |
| `domain_complexity` | 领域难度。 |
| `workflow_depth` | 预计工作流深度。 |
| `tool_risk` | 工具副作用风险。 |
| `context_need` | 需要多少项目上下文。 |
| `model_tier` | 小模型/中模型/强模型。 |
| `operator_mode` | 自动、半自动、人工审批。 |

路由策略：

- 简单分类、翻译、检索：小模型。
- 结构化 plan：中模型或强模型。
- 关键 Builder build、复杂代码/工作流生成：强模型。
- 高风险工具调用：强模型 + approval_required。

### 5.1 Operator 模式

| operator mode | 含义 |
| --- | --- |
| `answer_only` | 只回答，不改文件、不改 draft。 |
| `plan_only` | 只生成 plan 或 patch preview。 |
| `guarded_edit` | 可写入，但必须 preview 和审计。 |
| `build_blockflow` | 调用 Builder Team 创建或修改 `BlockFlow`。 |
| `human_approval` | 高风险动作必须人工批准。 |

## 6. Builder Team 可替换为工作流

长期目标是 Builder-as-workflow：

```text
requirement intake
  -> plan
  -> template retrieval
  -> module build
  -> test generation
  -> run tests
  -> repair
  -> publish
```

这不是立刻替换 `WorkflowBuilder`。建议先把每一步产物对象化，再逐步把 coordinator loop 下沉为可执行 `BlockFlow`。

## 7. 记忆检索与权限

记忆检索分三步：

1. Query rewrite：把用户问题转成检索需求。
2. Source selection：按权限选择 docs、templates、reports、activity memory。
3. Context pack：生成本次请求的 `AssistantContextPack`。

权限规则：

- 用户能看到助手使用了哪些记忆。
- 用户能删除记忆。
- 用户能关闭某类记录。
- 敏感来源不能进入小模型。
- 检索结果必须可追溯，不允许只返回 embedding 命中而没有 source refs。

## 8. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `api.py` | 助手入口、memory 查询、自然语言 patch endpoint。 |
| `workflow_storage.py` | 保存 activity memory、draft patch history。 |
| `template_store.py` | 供助手检索模板和实验报告摘要。 |
| `builder.py` | 支持 plan-first 和 patch-first 调用。 |
| `permissions.py` | 文件系统代理和 passmode。 |
| 前端 Studio | 增加 assistant panel。 |

### 8.1 第一版最小实现

1. Assistant panel 只读问答。
2. 可检索 docs/stage/design/asset，而不是活动监视。
3. `AssistantContextPack` 记录引用来源。
4. 自然语言 draft patch 只生成 preview，不自动应用。
5. 文件系统代理只支持 read-only。

### 8.2 第二版扩展

- Activity memory opt-in。
- FileSystemProxy guarded write。
- DraftPatchPlan apply。
- 难度路由。
- 小模型处理翻译、分类、摘要。

### 8.3 第三版扩展

- Builder-as-workflow。
- 多天项目续接。
- 与 Template RAG、实验报告和 stage report 深度联动。

## 9. 风险与约束

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 活动记忆侵犯隐私 | 用户信任下降。 | opt-in、范围、保留时间、删除入口。 |
| 小模型读到敏感信息 | 数据泄露。 | 敏感来源排除，小模型只处理低风险内容。 |
| 文件系统代理误写 | 项目损坏。 | preview、patch、approval_required。 |
| 记忆污染当前任务 | 助手引用过时信息。 | source refs、时间戳、stage 优先级。 |
| 助手化范围过大 | 实现失焦。 | 第一版只读上下文助手，不做自动监控。 |

## 10. 实验切片

对应实验：

- E11：自然语言 Draft Patch。
- E12：难度路由。
- E13：活动记忆助手。
- E07：小模型翻译/中转。

第一批实验：

| 实验 | 最小样例 | 指标 |
| --- | --- | --- |
| E11 | 对一个已有 draft 做自然语言小改。 | 成功率、耗时、测试回归。 |
| E12 | 20 个不同难度请求。 | 路由准确率、成本、失败率。 |
| E13 | 读取 stage/design/asset 后回答续接问题。 | 找回率、引用准确率、隐私风险。 |
| E07 | 中英需求转写后生成 plan。 | 结构正确率、语义遗漏率、成本。 |

## 11. 验收标准

- 用户能问“几天前那个工作流实验结果是什么”，系统能引用对应报告。
- 文件写入必须有 preview 或 patch。
- 小模型任务不能绕过 task monitor boundary。
- 自然语言修改工作流后，`content_hash` 改变，测试门禁失效并要求重测。
- 助手回答必须展示引用来源或说明没有依据。
- 用户能查看和删除 activity memory。
- 敏感路径默认不进入 memory 和小模型上下文。
- E11/E12/E13 实验完成并生成 `.docx` 报告。

## 12. 完成证据

本设计已补齐：

- 记忆、上下文包和 draft patch 核心对象。
- Activity memory 和 FileSystemProxy 边界。
- 自然语言修改工作流的数据流。
- 难度路由与 operator mode。
- 记忆检索与权限规则。
- 阶段化实现路径。
- 风险与实验切片。
- 可执行验收标准。

因此本文件可以作为 Lilies 助手化、项目记忆和自然语言工作流修改能力的长期设计依据。

## 13. 引用资产

- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`
- `docs/intellectual-assets/asset_blockflow_language_system.md`
