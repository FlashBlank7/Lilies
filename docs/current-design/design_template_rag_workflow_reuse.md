# design_template_rag_workflow_reuse

## 1. 问题

复杂 `BlockFlow` 不应该每次从零搭建。Lilies 的长期优势应来自被验证过的 `Template`、模块和实验经验复用。

当前已有 TemplateStore 和模板展开能力，但还没有形成完整的 Template RAG 和自动创建 loop。

## 2. 设计目标

- 把已发布、已测试的 `WorkflowSpec` 和 module 作为可检索知识资产。
- Builder 先检索模板，再决定展开、组合或从零构建。
- 每次成功构建后，自动判断是否可沉淀为模板。
- 通过复用深度限制防止模板套模板失控。
- 支持模板市场展示。

## 3. Template RAG loop

建议流程：

1. `requirement_embedding`：把用户需求、验收目标、领域标签向量化。
2. `template_retrieve`：检索候选模板。
3. `template_rank`：结合语义相似度、质量分、最近测试、工具需求、领域标签排序。
4. `template_expand`：展开候选模板为 draft。
5. `adapt_patch`：按需求做局部修改。
6. `test_run`：运行 mandatory tests。
7. `repair`：失败后走节点级或模块级修复。
8. `publish_or_ready`：通过后发布或 ready。
9. `reindex`：成功案例进入模板候选或更新质量分。

## 4. 模块化复用

定义 `WorkflowModule`：

| 字段 | 含义 |
| --- | --- |
| `module_id` | 稳定 ID。 |
| `module_name` | 模块名。 |
| `io_contract` | 输入输出 contract。 |
| `workflow_spec` | 模块内部图。 |
| `test_suite` | 模块级测试。 |
| `quality_score` | 质量分。 |
| `reuse_depth` | 当前复用深度。 |
| `source_application_id` | 来源。 |

Assembly workflow 只接模块的 I/O，不关心内部细节。

## 5. 复用深度

初始建议：

- 模板展开默认 `max_reuse_depth = 1`。
- 模块内部可以使用模板，但总深度不得超过 2。
- 超过深度时，Builder 必须内联展开或停止并请求人工确认。

复用深度不是越高越好。过深会造成：

- 测试定位困难。
- 运行成本难估计。
- Harness 边界不清。
- 模板市场质量分失真。

## 6. 模板市场最小展示

第一版模板市场只需要展示：

- 模板名和领域。
- 解决的问题。
- 输入输出。
- 质量分。
- 最近测试时间。
- 使用次数。
- 是否含外部工具。
- 是否含 soft harness blocks。

不要先做复杂商业化。先让维护人员能判断“这个模板是否值得复用”。

## 7. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `template_store.py` | 增加语义检索、质量分更新、module 类型。 |
| `template_models.py` | 增加 `TemplateKind`、`io_contract`、`reuse_depth`。 |
| `builder.py` | 强制先检索模板，再决定构建策略。 |
| `workflow_runtime.py` | 稳定 workflow-as-tool/module-as-tool 调用证据。 |
| 前端 Studio | 增加模板市场列表和模板展开预览。 |

## 8. 验收标准

- Builder 对复杂需求能返回候选模板列表和选择理由。
- 展开模板后仍可编辑、测试、发布。
- 模板复用不会绕过 `tested_hash == content_hash`。
- 模板市场能显示质量、测试和工具风险。
- E05/E06 实验完成并生成 `.docx` 报告。

## 9. 引用资产

- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
- `docs/intellectual-assets/asset_blockflow_language_system.md`
