# plan_evolution_flywheel_closure

## 1. 目标

修复 Lilies 元认知系统的六个断裂环节，让 Builder Team 搭建的工作流能真正反哺模板市场，实现"使用越多 → 模板越强 → Builder 越准"的自进化闭环。

## 2. 范围

**包含**：
- 将候选源从 `tracker.extract_workflow()`（决策树推导）切换到 `draft.snapshot.workflow`（真实产物）
- `merge_workflow_graph()` 实际合并工作流图
- 基于真实 WorkflowSpec 的 EvolutionGate 门控
- `TemplateMeta` 质量追踪字段（success_rate, evolution_history）
- `quality_score` 引入 success_bonus

**不包含**：
- Platform Harness 硬约束
- 前端模板市场 UI
- 跨框架进化验证

## 3. 关键决策

- 候选源切换：DecisionTracker 的 extract_workflow() 产生决策树风格工作流（llm+if_else+template+aggregator），与 Builder 实际搭建的结构完全不同——必须直接使用 draft.snapshot.workflow
- 图合并策略：保留模板原有节点，追加候选的增量节点和边，通过 type+title 计数处理重复
- 门控从 DecisionPoint 语义切换到 WorkflowSpec 结构分析

## 4. 实现路径

1. `template_models.py`: 新增 success_rate, total_uses, total_successes, last_validated_at, evolution_history
2. `evolution_engine.py`: 新建 EvolutionEngine + EvolutionGate
3. `merge_engine.py`: merge_workflow_graph() + merge() 真合并 + _compute_similarity() 增强
4. `extraction_gate.py`: 保留旧 ExtractionGate 向后兼容
5. `template_store.py`: snapshot(), rollback(), evolve()
6. `builder.py`: build_metadata 增强
7. `api.py`: _auto_extract_from_build 重写 + SSE 事件

## 5. 依赖设计

- `docs/intellectual-assets/asset_harness_llm_composite.md` — 优化投向组合层
- `docs/intellectual-assets/asset_blockflow_language_system.md` — WorkflowSpec vs BlockFlow 术语

## 6. 验收标准

- [x] 全量测试 61 passed, 0 failed
- [x] Lint clean
- [x] EvolutionGate 复杂度门控正确拒绝简单工作流
- [x] merge_workflow_graph 正确处理节点去重和 ID 冲突
- [x] merge() 实际修改工作流图
- [x] EvolutionEngine.evolve_or_create 正确分派进化/新建/拒绝路径
