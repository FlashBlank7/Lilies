# design_evolution_pipeline_blockflow

## 1. 问题

进化流水线（接收 Builder 产出的候选工作流 → 门控检查 → 相似度匹配 → 合并或新建模板）是一个纯 Harness（确定性）操作。在 V1.1 中，它以 Python 函数 `EvolutionEngine.evolve_or_create()` 的形式存在，通过 `_auto_extract_from_build` 的后台任务调用。

这违背了 Lilies 的核心架构原则：**任何 Harness+LLM 复合体应能以平台的积木系统表达。** 进化流水线本身就是一个 Harness 系统——它应该是一个 BlockFlow。

## 2. 设计目标

- **目标**：进化流水线作为 `templates/evolution_pipeline.json` BlockFlow 模板存在
- **非目标**：不在此阶段将进化纳入 task monitor boundary；不引入新的积木类型
- **边界**：EvolutionEngine 的 Python 实现保留为后端引擎，通过 Tool 注册桥接到 BlockFlow

## 3. 模块边界

```
┌─────────────────────────────────────────────────┐
│              BlockFlow 层                        │
│  templates/evolution_pipeline.json               │
│  start → tool(evolution_gate) → if_else → ...   │
└──────────────────────┬──────────────────────────┘
                       │ tool_name="evolution_gate"
┌──────────────────────┴──────────────────────────┐
│              Tool 桥接层                         │
│  tools/core.py: EvolutionGateTool                │
│  接收 JSON WorkflowSpec → 调用 EvolutionEngine   │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────┴──────────────────────────┐
│              Python 引擎层                        │
│  evolution_engine.py: EvolutionEngine             │
│  merge_engine.py: MergeEngine                    │
│  template_store.py: TemplateStore                │
└─────────────────────────────────────────────────┘
```

## 4. 数据流

```
_auto_extract_from_build (api.py)
  → 读取 draft.snapshot.workflow
  → 序列化为 JSON
  → 创建 WorkflowRun(evolution_pipeline, inputs={candidate_json, requirement, build_id})
  → WorkflowRuntime 执行 BlockFlow:
      start → 解析输入
      tool(evolution_gate) → EvolutionGateTool.execute()
        → EvolutionEngine.evolve_or_create()
        → 返回 EvolutionResult JSON
      if_else → 根据 result.mode 分支
      template_transform → 格式化结果报告
      event_recorder → 记录进化事件
      end → 输出进化结果
```

## 5. 实现方案

### EvolutionGateTool

```python
class EvolutionGateInput(BaseModel):
    candidate_json: str   # 序列化的 WorkflowSpec
    requirement: str      # Builder 原始需求
    build_id: str         # 来源构建 ID

class EvolutionGateTool(Tool):
    name = "evolution_gate"
    mutating = True  # 修改模板市场
    # execute() → EvolutionEngine.evolve_or_create() → EvolutionResult JSON
```

### 进化模板结构

```
start(candidate_json, requirement, build_id)
  → tool(evolution_gate): 执行完整进化流水线
  → if_else(mode_check): 三个分支
    → [evolved]  template_transform: "模板进化成功"
    → [created]  template_transform: "新模板已创建"
    → [rejected] template_transform: "被门控拒绝"
  → event_recorder: 记录进化结果
  → end: 输出 evolution_result + mode + template_name
```

### 推荐飞轮闭合

```
Builder._run() post-build:
  if state.expanded_from_template:
    TemplateStore.record_usage(name, success=status=="published")
      → total_uses += 1
      → if success: total_successes += 1
      → success_rate = total_successes / total_uses
```

### 家族感知相似度

```python
# merge_engine.py
family_a = {get_family(t) or t for t in a_types}
family_b = {get_family(t) or t for t in b_types}
family_sim = Jaccard(family_a, family_b)
type_sim = 0.6 * raw_type_jaccard + 0.4 * family_sim
```

## 6. 引用的智力资产

- `docs/intellectual-assets/asset_harness_llm_composite.md` — 原语即耦合，自反性要求
- `docs/intellectual-assets/asset_blockflow_language_system.md` — BlockFlow vs WorkflowSpec 定义
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md` — 进化任务未来需纳入 task monitor

## 7. 风险

- EvolutionGateTool 在 workflow 中执行时会访问 TemplateStore（修改模板市场），如果并发运行多个进化流水线，需要依赖 TemplateStore 的线程安全性
- 进化流水线自身是确定性的（无 LLM），但如果未来增加了 LLM 步骤，需要 structural_only 测试隔离

## 8. 验收标准

- [x] evolution_pipeline 模板通过 validate_workflow() 零错误
- [x] EvolutionGateTool 正确返回 EvolutionResult JSON
- [x] record_usage() 在构建成功后更新 success_rate
- [x] family-aware similarity 同家族积木获得非零相似度
- [x] 全量测试 61 passed, 0 failed
