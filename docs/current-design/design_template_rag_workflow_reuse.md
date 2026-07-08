# design_template_rag_workflow_reuse

状态：已完成  
对应 plan：`docs/workingon/plan_apply_lilies_design_notes_2026_07_08.md`  
完成日期：2026-07-08  
设计性质：下一阶段实现设计，不直接修改后端代码

## 1. 问题

复杂 `BlockFlow` 不应该每次从零搭建。Lilies 的长期优势应来自被验证过的 `Template`、模块和实验经验复用。

当前已有 TemplateStore 和模板展开能力，但还没有形成完整的 Template RAG 和自动创建 loop。

## 2. 设计目标

- 把已发布、已测试的 `WorkflowSpec` 和 module 作为可检索知识资产。
- Builder 先检索模板，再决定展开、组合或从零构建。
- 每次成功构建后，自动判断是否可沉淀为模板。
- 通过复用深度限制防止模板套模板失控。
- 支持模板市场展示。

非目标：

- 不在第一版做完整商业化模板市场。
- 不让模板复用绕过 draft、test 和 publish 门禁。
- 不要求所有成功 `BlockFlow` 自动进入模板库；必须经过质量筛选。
- 不把向量检索当作唯一检索方式；第一版可以用 tags、metadata 和结构特征混合检索。

## 3. 核心对象

### `TemplateIndexRecord`

模板检索不应直接扫完整 `WorkflowSpec`。需要单独的索引记录。

| 字段 | 含义 |
| --- | --- |
| `template_id` | 模板 ID。 |
| `template_kind` | `workflow` / `module` / `agent_loop` / `tool_bundle`。 |
| `title` | 模板名。 |
| `problem_statement` | 解决的问题。 |
| `domain_tags` | 领域标签。 |
| `capability_tags` | 能力标签，如 summarize、retrieve、publish。 |
| `io_contract` | 输入输出摘要。 |
| `node_type_summary` | 节点类型摘要。 |
| `tool_risk_summary` | 工具和外部 API 风险。 |
| `harness_summary` | soft harness block 与 Platform Harness 要求。 |
| `quality_score` | 当前质量分。 |
| `last_tested_at` | 最近测试时间。 |
| `embedding_ref` | 向量索引引用，可为空。 |

### `TemplateReuseDecision`

Builder 检索模板后必须说明为什么用或不用。

| 字段 | 含义 |
| --- | --- |
| `decision` | `reuse` / `adapt` / `compose` / `reject` / `build_from_scratch`。 |
| `candidate_template_ids` | 候选模板。 |
| `selected_template_ids` | 选中的模板。 |
| `reason` | 选择理由。 |
| `expected_changes` | 需要做的适配。 |
| `risk` | 复用风险。 |
| `required_tests` | 复用后必须运行的测试。 |

## 4. Template RAG loop

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

### 4.1 控制流

```text
BuildPlanSpec
  -> template_retrieve
  -> TemplateReuseDecision
  -> template_expand / module_compose / build_from_scratch
  -> adapt_patch
  -> draft_validate
  -> test_run
  -> repair or publish_or_ready
  -> reindex_candidate
```

控制流要求：

- 检索发生在 `BuildPlanSpec` 之后、draft mutation 之前。
- 复用决策必须写入 Build events。
- 展开模板后仍是 draft，必须重新计算 `content_hash`。
- 模板展开后的 `tested_hash` 为空，不能继承模板原来的测试结果。
- 只有当前 draft 的测试通过后才允许发布。

### 4.2 检索策略

第一版不依赖复杂向量数据库。建议混合检索：

| 信号 | 作用 |
| --- | --- |
| domain tags | 快速过滤领域。 |
| capability tags | 匹配任务能力。 |
| node type summary | 判断是否含需要的结构。 |
| io contract | 判断能否接入当前 plan。 |
| quality score | 排序加权。 |
| last tested | 过滤过旧模板。 |
| embedding similarity | 语义召回，作为可选增强。 |

### 4.3 失败回退

| 失败点 | 回退策略 |
| --- | --- |
| 没有候选模板 | 进入 plan-first from scratch。 |
| 候选模板质量低 | 只作为参考，不展开。 |
| 展开后结构验证失败 | 尝试 adapt patch；失败则拒绝模板。 |
| 测试失败但局部可修 | 进入 structured repair。 |
| 多个模板组合冲突 | 降低复用深度或请求人工确认。 |

## 5. 模块化复用

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
| `source_version_id` | 来源版本。 |
| `last_success_report` | 最近通过测试报告。 |
| `harness_requirements` | 运行该模块所需权限、预算、工具和沙盒要求。 |

Assembly workflow 只接模块的 I/O，不关心内部细节。

模块化复用要求：

- module 必须有独立测试。
- module 必须暴露 I/O contract。
- module 内部节点摘要必须可见，不能只显示一个黑箱名字。
- module 运行证据必须能回写到上层 `BlockFlow` 的 run report。

## 6. 质量分更新

`quality_score` 不应只是用户评分。建议由多项组成：

| 因子 | 说明 |
| --- | --- |
| `test_pass_rate` | 最近 N 次展开后的测试通过率。 |
| `repair_cost` | 平均 repair cycles 和人工修复次数。 |
| `runtime_cost` | 运行成本和耗时。 |
| `tool_evidence_score` | 是否提供真实工具证据和 URL 引用。 |
| `harness_score` | 是否声明权限、预算、沙盒、取消和审计要求。 |
| `reuse_success_count` | 被成功复用次数。 |
| `freshness` | 最近测试时间。 |

更新规则：

- 从模板展开的新 draft 通过测试后，增加 reuse success。
- 展开失败或需要大量 repair，降低质量分。
- 修改模板内容后，原质量分不能完全继承，必须重新测试。
- 过旧模板在排序中降权，但不自动删除。

## 7. 复用深度

初始建议：

- 模板展开默认 `max_reuse_depth = 1`。
- 模块内部可以使用模板，但总深度不得超过 2。
- 超过深度时，Builder 必须内联展开或停止并请求人工确认。

复用深度不是越高越好。过深会造成：

- 测试定位困难。
- 运行成本难估计。
- Harness 边界不清。
- 模板市场质量分失真。

强制规则：

- `max_reuse_depth` 是 Builder 和 TemplateStore 的共同约束。
- 超过深度时，必须内联展开或停止。
- 每次展开记录 parent template chain，便于 debug。

## 8. 模板市场最小展示

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

## 9. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `template_store.py` | 增加语义检索、质量分更新、module 类型。 |
| `template_models.py` | 增加 `TemplateKind`、`io_contract`、`reuse_depth`。 |
| `builder.py` | 强制先检索模板，再决定构建策略。 |
| `workflow_runtime.py` | 稳定 workflow-as-tool/module-as-tool 调用证据。 |
| 前端 Studio | 增加模板市场列表和模板展开预览。 |

### 9.1 第一版最小实现

1. `TemplateIndexRecord` 可由现有模板 metadata 生成。
2. `template_store.py` 支持基于 tags、kind、node summary 的检索。
3. `builder.py` 在 plan 后生成 `TemplateReuseDecision`。
4. 模板展开后必须重新测试，不继承 tested hash。
5. Template market 先展示只读列表。

### 9.2 第二版扩展

- 向量检索。
- module template 类型。
- 质量分自动更新。
- template chain 可视化。
- 自动 reindex 候选。

## 10. 风险与约束

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 模板污染新工作流 | 旧错误被复用。 | 检查最近测试和质量分。 |
| 复用深度失控 | 难以 debug 和估算成本。 | 强制 depth limit 和 template chain。 |
| 质量分失真 | 市场排序错误。 | 把测试通过率和 repair cost 纳入评分。 |
| 模板过早沉淀 | 垃圾模板增多。 | 只允许测试通过且复用价值明确的模板进入候选。 |
| 向量检索误召回 | Builder 选错模板。 | RAG 决策必须可解释，并允许 reject。 |

## 11. 实验切片

对应实验：

- E05：Template RAG。
- E06：模块化工作流复用深度。
- E01：Plan-first Builder。

第一批实验：

| 实验 | 最小样例 | 指标 |
| --- | --- | --- |
| E05 | 10 个模板库，3 个复杂需求。 | 命中率、展开后测试通过率、成本、速度。 |
| E06 | depth 0/1/2 的模块复用。 | 可读性、测试定位时间、运行耗时。 |
| E01 | 从零构建 vs plan+template 构建。 | 成功率、repair cycles、人工修复次数。 |

## 12. 验收标准

- Builder 对复杂需求能返回候选模板列表和选择理由。
- 展开模板后仍可编辑、测试、发布。
- 模板复用不会绕过 `tested_hash == content_hash`。
- 模板市场能显示质量、测试和工具风险。
- 复用深度有硬限制和 template chain 记录。
- 质量分更新规则不会把失败展开当作成功复用。
- E05/E06 实验完成并生成 `.docx` 报告。

## 13. 完成证据

本设计已补齐：

- 索引对象和复用决策对象。
- 检索、排序、展开、回退和再索引边界。
- 模块化复用约束。
- 质量分更新策略。
- 复用深度治理。
- 代码落点和阶段实现计划。
- 风险与实验切片。

因此本文件可以作为下一阶段实现 Template RAG、workflow module 复用和模板市场最小展示的设计依据。

## 14. 引用资产

- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`
- `docs/intellectual-assets/asset_harness_llm_composite.md`
- `docs/intellectual-assets/asset_blockflow_language_system.md`
