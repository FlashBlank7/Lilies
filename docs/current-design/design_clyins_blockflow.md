# design_clyins_blockflow

## 1. 问题

Clyins 是一个 AI 项目经理。它接收会议记录，自动提取行动项（谁/做什么/何时/依赖）、按依赖关系排序、生成格式化的日程表和会议纪要。

核心设计问题：**Clyins 应该作为什么形态存在？**

选项：
- A. 独立的 Python 服务
- B. Lilies 的一个 skill
- C. Lilies 的一个 BlockFlow 模板

选择 C，理由来自 `asset_harness_llm_composite.md`：

> 除了最基础的积木，剩下的任何形式都应该用工作流/模块的形式来循环实现。

Clyins 本身就是一个 Harness+LLM 复合体——确定性流程（抽取→拆解→分派→跟踪）包裹非确定性判断（LLM 理解内容、判断优先级）——因此它天然适合被实现为 BlockFlow。

## 2. 设计目标

- **目标**: Clyins 是一个可被 Builder Team 展开、可被 WorkflowRuntime 执行、可沉淀为 Template 的 BlockFlow
- **非目标**: Clyins 不引入新的积木类型；不依赖外部服务；MVP 不做语音输入
- **边界**: Clyins 模板只使用现有积木（start / template_transform / llm / task_dispatcher / human_input / end）

## 3. BlockFlow 架构

### 3.1 积木链

```
┌────────────┐   ┌────────────────┐   ┌──────────────────────┐
│   start    │   │template_transform│   │         llm          │
│ 会议记录    │──▶│   组合分析提示    │──▶│  Clyins 行动项提取     │
│ 团队背景    │   │                  │   │  structured_output:   │
│ 会议日期    │   │ template: 带     │   │  - summary            │
│            │   │ {{transcript}}   │   │  - schedule_table     │
│            │   │ {{team}} {{date}}│   │  - tasks[]            │
└────────────┘   └────────────────┘   └──────────┬───────────┘
                                                  │
                                                  ▼
┌────────────┐   ┌────────────────┐   ┌──────────────────────┐
│    end     │   │  human_input   │   │   task_dispatcher     │
│  输出成果   │◀──│   人工核验       │◀──│   依赖排序 (Kahn)      │
│ - schedule │   │  - approved     │   │   按依赖拓扑排序       │
│ - summary  │   │  - corrections  │   │   标记 ready/waiting   │
│ - tasks[]  │   │  - assign_lilies│   │                      │
└────────────┘   └────────────────┘   └──────────────────────┘
```

### 3.2 关键设计决策

**决策 1: LLM 直接生成 schedule_table**

`template_transform` 块只能通过 `{{ variable }}` 引用并 `str()` 渲染变量值，无法从 `[{...}, ...]` 数组生成 markdown 表格。

解决方案：在 LLM system prompt 中要求模型同时输出一个预格式化的 `schedule_table` 字段（markdown 表格字符串），format 节点直接引用 `extract.structured.schedule_table`。

**决策 2: task_dispatcher 兼容 title 字段**

LLM 倾向于输出 `title` 作为任务名字段名，但 `task_dispatcher` 原本只查 `name` → `subject`。

解决方案：在 `workflow_runtime.py` 的 task_dispatcher 执行逻辑中增加 `title` 作为第三回退：
```python
"name": task.get("name", task.get("subject", task.get("title", f"task-{idx}")))
```

**决策 3: human_input 自动恢复**

在 Web 上传流程中，`human_input` 节点（核验）会暂停工作流等待人工确认。但对 Web 用户来说，他们已经在最终结果页面进行 review，中间的暂停是不必要的摩擦。

解决方案：`POST /api/v1/clyins/run` 端点启动一个后台 asyncio task，轮询 run 状态，一旦检测到 `paused` 状态就自动调用 resume API 批准通过。用户在 Web 页面看到的最终结果即为已核验的完整输出。

### 3.3 LLM 输出 Schema

```json
{
  "summary": "会议摘要（一段话）",
  "schedule_table": "| 任务 | 负责人 | 截止日期 | 优先级 | 依赖 | 预估工时 |\n| ... |",
  "tasks": [
    {
      "name": "任务标题",
      "owner": "负责人",
      "deadline": "截止日期或时间描述",
      "dependencies": ["依赖的其他任务名称"],
      "priority": "high|medium|low",
      "estimated_hours": 数字
    }
  ]
}
```

## 4. 数据流

### 4.1 上传生成流程

```
用户粘贴会议文本到 Web 页面
  │
  ▼
POST /api/v1/clyins/run  {meeting_transcript, team_context, meeting_date}
  │
  ├─ 1. 创建 Application
  ├─ 2. expand_into_workflow("clyins", prefix="cy")
  ├─ 3. 逐个 add_node + add_edge 到 Draft
  └─ 4. create_run(inputs={...}, use_draft=True)
       │
       ▼
  WorkflowRuntime 执行 BlockFlow
       │
       ├─ start: 解析输入
       ├─ compose: 渲染分析 prompt
       ├─ extract: DeepSeek V4 Pro 提取 → JSON
       ├─ dispatch: Kahn 拓扑排序
       ├─ format: 渲染日程表
       ├─ verify: ⏸️ → 后台 auto-resume
       └─ end: 输出 {schedule, summary, tasks, verified, ...}
       │
       ▼
  前端轮询 GET /api/v1/runs/{run_id} → 展示结果
```

### 4.2 查看已有日程流程

```
用户输入 run_id → 前端 GET /api/v1/runs/{run_id}
  → 解析 outputs.schedule (markdown) → 渲染为 HTML
  → 解析 outputs.tasks → 渲染任务表格
```

## 5. 与 Lilies 的关系

Clyins 与 Lilies 形成**递归的自引用关系**：

```
Clyins (BlockFlow) ──调用──▶ Lilies Builder Team API ──生成──▶ 新的 BlockFlow
       ▲                                                           │
       │                                                           │
       └──────────── 生成的 BlockFlow 可沉淀为 Template ◀───────────┘
```

具体来说：
- Clyins 是 Lilies 上的一个 BlockFlow 模板
- Clyins 拆解出的任务，可以通过 `assign_to_lilies` 标志触发 Builder Team 自动创建对应的 BlockFlow
- Builder Team 搭建的工作流经过验证后可沉淀为新模板
- 新模板增强了 Builder Team 的能力，使其能更好地搭建 Clyins-like 工作流
- 形成 "使用越多 → 模板越强 → Builder 越准" 的飞轮

## 6. 部署架构

```
公网 (drone-swarm.nat100.top)
        │
        ▼
   natapp (authtoken: 3482d133aa61d164, lport: 8001)
        │
        ▼
   uvicorn :8001 (Lilies API, host: 0.0.0.0)
        │
        ├── GET  /run-view.html          ← Web 界面
        ├── POST /api/v1/clyins/run       ← 上传会议文本
        └── GET  /api/v1/runs/{run_id}    ← 获取日程结果
```

natapp 配置文件: `/home/jiangzhijun/natapp_config.ini`

与其他服务零冲突：现有的 natapp 进程（PID 2545, authtoken: dd965c2b459017d6, 隧道到 8080）和 nginx 容器（zkr-lab-erp-demo, :8080→:80）完全不受影响。

## 7. 文件清单

| 文件 | 角色 |
|------|------|
| `templates/clyins.json` | Clyins BlockFlow 模板定义 |
| `platform/backend/src/agent_platform/workflow_runtime.py` | task_dispatcher title 回退 |
| `platform/backend/src/agent_platform/api.py` | POST /api/v1/clyins/run 端点 |
| `platform/backend/src/agent_platform/workflow_models.py` | ClyinsRunRequest 模型 |
| `mobile_app/run-view.html` | Web 上传/查看界面 |
| `tests/test_workflow.py` | 3 个 Clyins 测试 |
| `demo_clyins.py` | 端到端演示脚本 |

## 8. 引用的智力资产

- `docs/intellectual-assets/asset_harness_llm_composite.md` — 原语即耦合：Clyins = Harness + LLM
- `docs/intellectual-assets/asset_blockflow_language_system.md` — Clyins 是一个 BlockFlow，底层是 WorkflowSpec
- `docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md` — auto-resume 任务遵循 task monitor 原则
- `docs/current-design/design_evolution_pipeline_blockflow.md` — BlockFlow 自我引用的先例
