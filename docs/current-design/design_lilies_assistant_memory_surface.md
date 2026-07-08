# design_lilies_assistant_memory_surface

## 1. 问题

Lilies 如果只像一个平台，就容易被理解为“画布 + 插件 + 一个 Builder”。用户真正想要的是类似 Codex/Claude Code 的使用体验：她能理解项目、记住长期上下文、调取文件和模板、帮用户继续几天前的工作。

这个方向很有吸引力，但风险也大：活动监视、文件系统封装和长期记忆都必须有明确权限和审计边界。

## 2. 设计目标

- 把 Lilies 升级成可调用的项目助手，而不只是后台平台。
- 让助手能检索项目记忆、模板、实验报告和阶段文档。
- 对文件系统访问做显式封装和权限控制。
- 用小模型承担低风险任务：翻译、分类、检索、路由、摘要。
- 根据任务难度选择 operator、workflow depth 和模型。

## 3. 产品面能力

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

### 自然语言修改工作流

用户在画布上说：

> 把这个小说生成流程加强设定遵循度，并把测试输出改成可读报告。

系统应生成 `DraftPatchPlan`，而不是重建整个 `WorkflowSpec`。

## 4. 难度路由

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

## 5. Builder Team 可替换为工作流

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

## 6. 代码落点

| 模块 | 改动方向 |
| --- | --- |
| `api.py` | 助手入口、memory 查询、自然语言 patch endpoint。 |
| `workflow_storage.py` | 保存 activity memory、draft patch history。 |
| `template_store.py` | 供助手检索模板和实验报告摘要。 |
| `builder.py` | 支持 plan-first 和 patch-first 调用。 |
| `permissions.py` | 文件系统代理和 passmode。 |
| 前端 Studio | 增加 assistant panel。 |

## 7. 验收标准

- 用户能问“几天前那个工作流实验结果是什么”，系统能引用对应报告。
- 文件写入必须有 preview 或 patch。
- 小模型任务不能绕过 task monitor boundary。
- 自然语言修改工作流后，`content_hash` 改变，测试门禁失效并要求重测。
- E11/E12/E13 实验完成并生成 `.docx` 报告。

## 8. 引用资产

- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md`
- `docs/intellectual-assets/asset_lilies_competitive_strategy.md`
- `docs/intellectual-assets/asset_blockflow_language_system.md`
