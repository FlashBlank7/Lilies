# plan_evolution_pipeline_as_blockflow

## 1. 目标

将进化流水线从 Python 函数（`_auto_extract_from_build` + `EvolutionEngine`）提升为平台自身的 BlockFlow 模板，实现"原语即耦合"的自反性要求：平台的核心能力应以自身的积木系统表达。

同时闭合推荐飞轮（模板使用→构建成功/失败→回写 success_rate）和引入家族感知相似度（MergeEngine 感知 block family 语义）。

## 2. 范围

**包含**：
- `EvolutionGateTool` 注册为核心工具，桥接 Python EvolutionEngine 到 BlockFlow
- `templates/evolution_pipeline.json` — 确定性进化流水线模板
- `BuildTeamState.expanded_from_template` + builder 回写 + `TemplateStore.record_usage()`
- `MergeEngine._compute_similarity()` 混入 40% family Jaccard

**不包含**：
- 进化流水线进入 task monitor boundary（下一阶段 Platform Harness 生产化）
- 降级信号（需要积累使用数据）

## 3. 关键决策

- 使用 `tool` 积木 + `evolution_gate` 工具（而非新建积木类型）桥接 Python 函数——遵循现有的 Tool 注册模式
- 进化模板使用纯确定性积木：start → tool → if_else → template_transform → event_recorder → end
- family Jaccard 与 type Jaccard 混合权重：60% raw types + 40% family grouping

## 4. 实现路径

1. `tools/core.py`: EvolutionGateInput + EvolutionGateTool，build_core_registry() 接受 template_store
2. `templates/evolution_pipeline.json`: 8 节点 9 边 BlockFlow
3. `workflow_models.py`: BuildTeamState.expanded_from_template
4. `builder.py`: template_expand 存储 + post-build 回写
5. `template_store.py`: record_usage(name, success)
6. `merge_engine.py`: family-aware _compute_similarity()
7. `api.py`: build_services() 先创建 templates 再传 build_core_registry()

## 5. 依赖设计

- `docs/intellectual-assets/asset_harness_llm_composite.md` — 组合层优化 + "原语即耦合"自反性
- `docs/intellectual-assets/asset_blockflow_language_system.md` — BlockFlow 定义
- `docs/current-design/design_evolution_pipeline_blockflow.md` — 具体设计

## 6. 验收标准

- [x] 全量测试 61 passed, 0 failed
- [x] Lint clean
- [x] evolution_pipeline 模板通过 build_block_registry().validate_workflow()
- [x] family-aware similarity: same-family 0.688, identical 1.000, different 0.300
- [x] record_usage() 在构建成功后更新 success_rate
