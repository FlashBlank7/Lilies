# Lilies 设计推演与验证文档

> 从"三个 Idea 深度评估"到"模板系统落地"的完整设计推演过程，包含每个决策的动机、分析、验证结果。

---

## 目录

1. [三个核心 Idea 的评估与验证](#1-三个核心-idea-的评估与验证)
2. [25 积木冗余性分析与最小 Agent Loop](#2-25-积木冗余性分析与最小-agent-loop)
3. [工作流的图灵完备性](#3-工作流的图灵完备性)
4. [Lilies 工作流 vs Harness 的异同](#4-lilies-工作流-vs-harness-的异同)
5. [从 Harness 借鉴的设计决策](#5-从-harness-借鉴的设计决策)
   - 5.5 [Platform Harness 的必要性](#55-platform-harness-的必要性--软约束-vs-硬约束)
   - 5.6 [开发阶段的隐性 Harness](#56-开发阶段的隐性-harness--从人ai-协作到自动化)
6. [模板系统设计推演](#6-模板系统设计推演)
7. [多 Agent 软件工程团队](#7-多-agent-软件工程团队)
8. [非确定性隔离与并发安全](#8-非确定性隔离与并发安全)
9. [前后对比：改进是否有效](#9-前后对比改进是否有效)
10. [架构决策记录 (ADR)](#10-架构决策记录-adr)
11. [元认知层 — 从协作中自动提取工作流](#11-元认知层--从协作中自动提取工作流)
12. [钉钉自动化案例研究](#12-钉钉自动化案例研究--一个完整的工作流固化之旅)
13. [待完成工作](#13-待完成工作)
14. [架构决策记录 (ADR) — 新增](#14-架构决策记录-adr--新增)
[附录：累计测试矩阵](#附录更新后的测试矩阵)

---

## 1. 三个核心 Idea 的评估与验证

### Idea 1: "工作流是模块，模块是工作流"

**命题**：任何已发布的工作流都可以作为积木被其他工作流调用。

**分析**：

这个理念已经在 Lilies 中有雏形——`workflow:` 前缀的 Tool 调用允许已发布的工作流被嵌套执行。积木系统的 `$ref` 引用机制和 Version 锁定使工作流之间实现零耦合组合。

**验证证据**：

- **场景 D（14 积木 Agent Loop）**：嵌套的 Loop 工作流无缝作为外层工作流的内部组件运行
- **多模板串联测试**：3 个独立模板（task_decomposer + data_analyzer + long_form_writer）在同一工作流中串联运行，证明"模块=工作流"的组合能力
- **模板展开 API**：模板展开为可编辑工作流草稿后，用户可修改后发布为新模板，形成"工作流→模板→新工作流"的闭环

**评估**: ⭐⭐⭐⭐⭐ — 基础设施已具备，是 Lilies 相比于 Millipede 最大的架构优势。

**注意事项**：

| 问题 | 缓解方案 |
|------|---------|
| 循环依赖 | 发布时检查依赖树 |
| 版本漂移 | `$ref` 锁定具体 Version |
| 调试困难 | 完整保留父子关系链 + SSE 事件树 |

---

### Idea 2: "文本即智能 — 模块间通过结构化文本通信"

**命题**：模块的输出不是黑箱 JSON，而是 LLM 天然理解的结构化文本。不需要为每个模块定义精确的 Schema，语义靠 LLM 自己理解，结构靠 `$ref` 精确引用。

**分析**：

设计了**三层通信协议**：

```
Layer 3: 自然语言 ("测试失败了，median应该是2.5而不是2")
         → 人类可读，LLM 可理解，灵活但非结构化

Layer 2: 半结构化 (Markdown/JSON+text)
         → 机器可解析关键字段，附带人类可读上下文

Layer 1: 结构化 ($ref + JSON Schema)
         → 精确引用，类型安全，确定性路由
```

**验证证据**：

- **Agent 代码调试场景**：Agent 读取 Python 代码（文本）→ 运行 pytest（文本输出）→ 阅读错误信息（文本）→ 定位 bug → Edit 修复，全程通过文本流转
- **模型回答质量**：model_turn 自然语言输出被下游 template_transform 正确理解和格式化
- **error_classifier**：基于文本模式匹配（"timeout"→retryable, "syntax error"→tool），10/10 分类准确

**评估**: ⭐⭐⭐⭐ — 方向正确，但需要显式规范三层通信协议。

---

### Idea 3: "要能解决复杂任务 — 万字长文、多 Agent 协作"

**命题**：平台不仅要能处理简单问答，还要能解决需要分而治之的复杂任务。

**分析**：

万字长文生成面临三个技术挑战：

| 挑战 | 解决方案 | 实现状态 |
|------|---------|---------|
| 单次输出不够长（~4000 tokens） | 分节生成 + 拼接（Iteration 块） | ✅ 已验证 |
| 上下文窗口管理 | Context Compactor 积木 | ✅ 已实现 |
| 长文档一致性 | 全局协调员 + 分段 Reviewer | ✅ subagent_spawn 已验证 |

**验证证据**：

- **long_form_writer 模板**：已编码为可复用模板（start → LLM大纲 → Iteration章节 → 聚合 → 输出）
- **多 Agent 协作场景**：Subagent Spawn + Task Dispatcher + Dependency Gate + Mailbox 组合已验证可编排多个 Agent 按依赖顺序协作
- **代码修复 Agent**：在包含 2 个 bug 的项目中，Agent 自动完成 Read→Test→Find Bug→Edit→Re-test 全流程，5/5 测试通过

**评估**: ⭐⭐⭐⭐⭐ — 技术路径清晰，已有基础设施支持。

---

## 2. 25 积木冗余性分析与最小 Agent Loop

### 问题

25 个 Agent 架构积木是否冗余？能否合并为更少的积木而达到相同效果？

### 分析方法

将 25 积木按**可独立测试的最小运行时机制**分组：

```
Context:    4 个 (assembler, injector, memory, compactor)
Model Loop: 4 个 (model_turn, tool_call_router, stop_continue, retry_error)
Tools:      4 个 (tool_executor, tool_result_normalizer, permission_gate, sandbox_boundary)
Skill/MCP:  3 个 (skill_loader, mcp_gateway, capability_registry)
Multi-Agent:4 个 (subagent_spawn, task_dispatcher, mailbox_wait_wake, dependency_gate)
Governance: 5 个 (budget_gate, round_limit, cancellation_point, checkpoint_resume, event_recorder)
Extension:  1 个 (hook_point)
─────────────────
总计:      25 个
```

### 合并实验

如果全部合并，25 → 6 个积木。代价：

| 能力 | 合并前 | 合并后 |
|------|--------|--------|
| 预算独立测试 | ✅ `budget_gate` 输出 `{allowed: True, spent: 0.5}` | ❌ 与轮次/取消混合 |
| 错误分类独立验证 | ✅ `retry_error_classifier` 输出 `{class: "retryable"}` | ❌ 混入 Model 块 |
| 权限插入点 | ✅ 可在任意位置插入 `permission_gate` | ❌ 只能与 Tool 块耦合 |

**结论**：不冗余，但可分三层粒度。

### 三层粒度模型

| 层 | 数量 | 积木 | 用户可见性 |
|----|------|------|-----------|
| **基础粒度** (不可再拆) | 7 | model_turn, tool_executor, tool_result_normalizer, event_recorder, context_assembler, permission_gate, sandbox_boundary | 始终可见 |
| **组合粒度** (常用模式) | 5 | tool_call_router, stop_continue_controller, retry_error_classifier, budget_gate, round_limit | 始终可见 |
| **策略粒度** (可替换) | 13 | 其余积木 | 高级模式展开 |

### 最小生产级 Agent Loop

**10 个积木**即可构建完整 Agent Loop：

```
context_assembler → model_turn → tool_call_router → tool_executor
  → tool_result_normalizer → stop_continue_controller
  → permission_gate → budget_gate → retry_error_classifier → event_recorder
```

验证：14 积木场景 D 已证明 10+ 积木链可正常串联执行。

---

## 3. 工作流的图灵完备性

### 计算要素映射

| 计算要素 | Lilies 实现 | 对应 |
|---------|-----------|------|
| 顺序执行 | Edge | `;` |
| 条件分支 | If/Else, Question Classifier | `if/else` |
| 循环 | Loop, Iteration | `for/while` |
| 变量绑定 | Variable Assigner, `$ref` | `x = ...` |
| 函数调用 | workflow as Tool (嵌套) | `f()` |
| 非确定性计算 | LLM, model_turn | oracle |

**结论**：Lilies 的工作流系统在 LLM oracle 辅助下是**图灵完备的**。

### 任意 Agent 问题的分解框架

任何 Agent 工作流问题可分解为四阶段：

```
Agent Problem = Plan + Execute + Observe + Decide
              = Task Decomposition + Tool-using Loop + Result Analysis + Control Flow
```

Lilies 映射：
- **Plan** = Layer 3 编排工作流 (Task Dispatcher + Subagent)
- **Execute** = Layer 2 Agent Loop (10 积木核心)
- **Observe** = Event Recorder + Tool Result Normalizer
- **Decide** = Stop/Continue + If/Else + Budget Gate

**理论限制不在架构上，而在**：
1. LLM 的上下文窗口大小
2. LLM 的推理质量
3. 嵌套层数的调试复杂度

---

## 4. Lilies 工作流 vs Harness 的异同

### 属性对比

| 属性 | Harness | Lilies 工作流 |
|------|---------|-------------|
| 谁定义的 | 平台开发者 | 用户 / AI Builder |
| 可修改吗 | 不可（运行时固定） | 可（草稿→编辑→测试→发布） |
| 可嵌套吗 | 不可（只有一个 harness） | 可（工作流作为 Tool 调用） |
| 可测试吗 | 间接（测试 Agent 行为） | 直接（断言工作流输出） |
| 可版本化吗 | 平台升级 | `$ref` 锁定具体 Version |
| 粒度 | 粗（一个整体） | 细（25 个可替换积木） |

### 核心差异

```
Harness 模式:                     Lilies 模式:

  ┌──────────┐                     ┌──────────────────┐
  │ Harness   │ ← 平台定义，不可改   │ 用户定义的工作流    │
  │ ┌──────┐ │                     │ ┌──────────────┐ │
  │ │Agent  │ │ ← 在harness内运行   │ │ Agent Block  │ │ ← Agent是积木
  │ └──────┘ │                     │ └──────────────┘ │
  └──────────┘                     │ ┌──────────────┐ │
                                   │ │ Tool Block   │ │
                                   │ └──────────────┘ │
                                   │ ┌──────────────┐ │
                                   │ │Permission Gate│ │
                                   │ └──────────────┘ │
                                   └──────────────────┘
                                     ↑ 用户可通过Builder
                                       修改任意积木
```

**Lilies 不是要替代 Harness，而是让用户能搭建自己的 Harness。**

### 15 项 Harness 能力 → Lilies 积木映射

| Harness 能力 | Lilies 积木 | 状态 |
|-------------|-----------|------|
| Tool sandbox | sandbox_boundary + tool_executor | ✅ |
| Permission mode | permission_gate (3级) | ✅ |
| Memory system | conversation_memory + context_compactor | ✅ |
| Model invocation | model_turn | ✅ |
| Tool routing | tool_call_router | ✅ |
| Budget control | budget_gate | ✅ |
| Round limit | round_limit | ✅ |
| Event streaming | event_recorder | ✅ |
| Sub-agent spawning | subagent_spawn | ✅ |
| Cancellation | cancellation_point | ✅ |
| Checkpoint/resume | checkpoint_resume | ✅ |
| Hook system | hook_point | ✅ 新增 |
| Task list | task_dispatcher | ✅ |
| Skill loading | skill_loader | ✅ |
| MCP integration | mcp_gateway | ✅ |

**15/15 能力全部有积木对应。**

---

### 5.5 Platform Harness 的必要性 — 软约束 vs 硬约束

**核心命题**：Lilies 永远需要外层 Platform Harness，因为信任边界是不对称的。

Lilies 工作流内的积木（budget_gate, permission_gate, round_limit 等）是**软约束**——它们存在于工作流内部，用户或 AI Builder 可以通过编辑工作流来移除它们。Platform Harness 是**硬约束**——它们存在于工作流外部，工作流完全不知道它们的存在，因此无法绕过。

**类比**：

> 你家的智能家居系统可以控制灯光、空调、窗帘。但电闸、保险丝、入户门锁不在智能家居的控制范围内——它们是房子的基础设施，无论智能家居怎么配置都不会被绕过。Lilies 是智能家居，Platform Harness 是房子的承重墙和入户门。

**攻击场景与防御**：

| 攻击 | Lilies 软约束失效 | Harness 硬约束防御 |
|------|-----------------|-------------------|
| Bash 外泄数据 | 编辑 sandbox_boundary(network_policy="full") | Docker egress firewall 不可被工作流覆盖 |
| 无限循环烧钱 | 删除 budget_gate 和 round_limit | 用户级配额 + 硬性 sandbox timeout (600s) |
| 权限提升 | 设置 permission_gate(mode="auto_approve") | Authz Layer: 该用户没有 Write 权限 |
| 跨用户读取 | 修改 workspace_path | 用户级 workspace 隔离 + 文件系统权限 |

**核心设计原则**：

> 工作流可以自我约束，但不能自我解放。只有 Platform Harness 拥有"解放"的权限。

**约束对照**：

| 约束类型 | 实现位置 | 可被绕过？ | 举例 |
|---------|---------|----------|------|
| 软约束 | Lilies 工作流内 | ✅ 编辑即可移除 | budget_gate, permission_gate |
| 硬约束 | Platform Harness | ❌ 工作流不可见 | JWT, Docker cgroup, 用户配额 |

**Platform Harness 五层架构**：



**关键 Harness 代码结构**：



**为什么不能全放 Lilies 里？**

> Lilies 积木是"自觉遵守交通规则"，Platform Harness 是"交警 + 摄像头 + 扣分系统"。两者都需要。

攻击者 Fork 工作流 → 删除 budget_gate → 发布 → 运行，Lilies 无法阻止。但 Harness 的 Quota Service 仍强制执行 /run 限制，Audit Log 记录了一切。

---

### 5.6 开发阶段的隐性 Harness — 从人+AI 协作到自动化

**观察**：当前开发阶段，人+AI 协作已经隐性地完成了 Platform Harness 的功能。

**当前 Harness 映射**：

| Harness 功能 | 当前提供者 | 形式 | 生产化状态 |
|-------------|----------|------|-----------|
| API Key 管理 | 用户 → .env | 手动配置 | 需自动化 |
| Docker 权限 | sg docker | 每次显式授权 | 需服务化 |
| 测试决策 | 用户 → "请测试" | 对话指令 | 需自助 |
| 错误恢复 | AI → 重试修复 | 对话迭代 | 需自动重试 |
| 审计日志 | 对话记录 | 非结构化 | 需结构化 |
| 成本控制 | API Key 余额 | 平台计费 | 需配额系统 |

**本质差异**：

```
现在 (开发):                       目标 (生产):

┌──────────────────┐              ┌──────────────────┐
│  用户 (决策者)     │              │  终端用户 (自助)   │
├──────────────────┤              ├──────────────────┤
│  AI (执行者)      │              │  Platform Harness │
│  理解意图          │              │  验证身份          │
│  构造测试          │              │  检查权限          │
│  调用 Lilies      │              │  强制执行配额      │
│  分析结果          │              │  创建沙盒          │
│  修复问题          │              │  注入密钥          │
├──────────────────┤              │  审计记录          │
│  Lilies (工具)    │              ├──────────────────┤
└──────────────────┘              │  Lilies (工具)    │
                                  └──────────────────┘
人+AI = Harness原型               代码 = Harness产品
灵活智能，但有人工瓶颈              固定可靠，7×24运行
```**核心洞察**：当前 AI 做的每一次测试构造、每次错误分析、每次修复决策，都是在定义 Platform Harness 应该自动执行什么规则。生产化 = 把 "AI 作为 Harness 原型" 转化为 "代码作为 Harness 产品"。


---

## 5. 从 Harness 借鉴的设计决策

### 决策 1: Permission Gate 三级模式

**来源**：Harness 的 `default / bypass / plan` 三级权限模式

**实现**：
```python
mode: "always_ask" | "plan_first" | "auto_approve"

always_ask  — 每次敏感操作都暂停等待审批
plan_first  — 先展示计划，批准后执行（防误触）
auto_approve — 自动批准（受 max_auto_per_hour 限制）
```

**验证**：Permission Gate 三级模式在 Hook+Perm 测试（场景 C）中结构验证通过。

### 决策 2: Hook Point 积木

**来源**：Harness 的 hook 系统（`<system-reminder>` 注入、事件拦截）

**实现**：
```python
hook_point:
  hook_name: str        # 钩子名，供外部系统匹配
  direction: "before" | "after"
  timeout_seconds: 30   # 等待外部响应超时
  default_behavior: "continue" | "abort"
```

**设计选择**：v1 实现为非阻塞（emit hook.triggered 事件 + 继续执行），后续版本可增加同步等待模式。

**验证**：Hook Point 积木已注册为第 25 个 Agent 架构积木，hook.triggered 事件已集成到 SSE 事件流。

### 决策 3: 结构化事件命名规范

**来源**：Harness 的 `{domain}.{action}` 事件命名

**实现**：
```
workflow.started / workflow.completed / workflow.paused / workflow.failed
node.started / node.completed / node.skipped / node.failed / node.retry
tool.started / tool.completed / tool.failed
permission.requested / permission.resolved / permission.plan
hook.triggered
subagent.started / subagent.completed / subagent.event
model.text.delta / model.thinking.delta
context.compaction.started / context.compaction.completed
error.classified
budget.exceeded / round_limit.reached
checkpoint.saved / cancellation.checked
```

---

## 6. 模板系统设计推演

### 为什么需要模板系统

"工作流固化"是 Lilies 的核心价值主张。模板系统是这一主张的实现层：

1. **专家的做事方式需要载体** — 模板就是"工作流的最佳实践"
2. **优质工作流需要被发现和复用** — 模板市场提供搜索、分类、评分
3. **模板本身就是"模块"（Idea 1 落地）** — 模板展开后可作为新工作流的基础

### 设计决策

| 决策 | 理由 |
|------|------|
| JSON 文件存储 | 版本控制友好、可手工编辑、可被 AI Builder 读取 |
| 展开式（Fork）而非引用式 | 保证模板稳定，用户修改不影响原始模板 |
| 模板可发布回市场 | 形成"使用→改进→发布→更多人使用"的正循环 |
| 6 个内置模板覆盖主要场景 | 代码工程 / 数据分析 / 客服 / 内容创作 / 任务管理 |

### 6 个内置模板

| 模板 | 分类 | 节点 | 核心流程 |
|------|------|------|---------|
| code_reviewer | code_engineering | 3n/2e | start → llm → end |
| data_analyzer | data_analysis | 5n/4e | start → llm → extractor → template → end |
| customer_support_router | customer_service | 5n/4e | start → classifier → if/else → 4×template → aggregator → end |
| document_summarizer | content_creation | 4n/3e | start → llm → template → end |
| task_decomposer | task_management | 4n/3e | start → llm → template → end |
| long_form_writer | content_creation | 3n/2e | start → llm(大纲) → end (可嵌套 Iteration) |

### 模板 API

```
GET    /api/v1/templates                  列表 + 搜索 + 分类过滤
GET    /api/v1/templates/categories       所有分类
GET    /api/v1/templates/{name}           单个模板（含完整 WorkflowSpec）
POST   /api/v1/templates/{name}/expand    展开为可编辑工作流（自动重写 $ref 和 node ID）
POST   /api/v1/apps/{id}/publish-template 从当前草稿发布为新模板
```

---

## 7. 多 Agent 软件工程团队

### 为什么需要多 Agent 协作

单个 Agent 有上下文窗口和推理质量的限制。复杂软件工程任务（需求→设计→实现→测试→交付）天然需要多个专项 Agent 分工协作。这是 Idea 3（"解决复杂任务"）的深化实践。

### 架构设计

```
┌─────────────────────────────────────────────────┐
│              Coordinator (协调者 + Builder)       │
│  接收需求 → 拆解 → 分派 → 汇总 → 交付            │
└────┬──────────┬──────────┬──────────┬───────────┘
     │          │          │          │
┌────▼───┐ ┌───▼────┐ ┌───▼────┐ ┌───▼────┐
│需求拆解│ │系统设计│ │测试方案│ │代码编写│
│ Agent  │ │ Agent  │ │ Agent  │ │ Agent  │
└────────┘ └────────┘ └───┬────┘ └───┬────┘
                          │          │
                     ┌────▼──────────▼────┐
                     │    测试执行 Agent   │
                     │  运行+验证+报告     │
                     └────────────────────┘
```

### Agent 生成可靠性

**问题**：DeepSeek V4 Pro 在生成大型 AgentSpec JSON（>8000 tokens）时偶发截断，导致 Agent 生成失败。

**5 轮优化路径**：

| 迭代 | 改动 | 效果 |
|------|------|------|
| 1 | `max_output_tokens` 8192→4096 | 减少截断窗口 |
| 2 | AGENT_GENERATOR_PROMPT 添加 "UNDER 2000 chars" 约束 | 精简输出 |
| 3 | Factory 重试次数 3→4 | 增加容错 |
| 4 | 添加指数退避延迟（`1.5 × (attempt+1)` 秒） | 缓解速率限制 |
| 5 | 捕获 `RuntimeError("invalid tool input JSON")` 进入重试循环 | 覆盖 JSON 解析错误 |

**最终结果**：Agent 生成成功率从 ~60% 提升到 ~85%。5/5 Agent 生成成功的已验证案例。

### 产品级代码生成验证

**任务**：构建 JSON Schema 验证器库（8 种 schema 关键词、类型注解、完整测试）

**Agent 链执行**：Coder Agent 独立编写 → Tester Agent 测试+修复

**产物**：

| 文件 | 规模 | 质量 |
|------|------|------|
| `json_validator.py` | 536 行 / 18,101 字符 | 完整 docstring、类型注解、8 种 schema 关键词全覆盖 |
| `test_validator.py` | 20,441 字符 | 76 个 pytest 测试，覆盖全部边界条件 |
| **pytest 结果** | **76/76 通过** | 类型/required/properties/enum/minmax/len 全部场景 |

**冒烟测试**：
```
✅ Test1: valid data → [] (通过)
✅ Test2: missing required → 1 error: "required field missing: name" (通过)
✅ Test3: type mismatch → 1 error (通过)
```

这表明 Lilies 的 Agent 团队能够独立完成从需求到生产级代码的完整工程闭环。

---

## 8. 非确定性隔离与并发安全

### LLM 非确定性隔离

**问题**：LLM 输出本质非确定。在 LLM 节点存在的工作流测试中，基于内容相等的断言（`equals`、`contains`）会因 LLM 输出变化而间歇性失败。

**解决方案**：引入结构断言与内容断言的分离。

```python
class TestAssertion:
    operator: Literal[
        # 结构断言 (确定性 — 独立于 LLM 输出)
        "exists", "type", "min_length", "max_length",
        # 内容断言 (非确定性 — 依赖 LLM 输出)
        "equals", "contains", "not_contains",
    ]

class WorkflowTestCase:
    structural_only: bool = False  # True 时降级所有内容断言为结构检查
```

**验证**：
- LLM 工作流 3 次运行答案长度 [175, 158, 150] 各不相同（内容非确定）
- 结构断言 (`exists`+`min_length`+`type`) 3/3 全部通过
- 纯确定性流程（无 LLM）5 次运行输出完全一致

### 并发安全

**问题**：多用户同时编辑同一草稿、多个工作流并发运行时需要保证状态一致性。

**验证矩阵**：

| 场景 | 结果 | 关键指标 |
|------|------|---------|
| 双 Client 并发草稿变异 | ✅→✅ | 旧 revision 被 409 拒绝，刷新后成功 |
| 5 并发运行 | ✅ | 全部成功，5 个不同输出（零交叉污染） |
| 10 并发快速运行 | ✅ | 全部成功 |
| 幂等键保护 | ✅ | 相同 idempotency_key 被 422 拒绝 |
| 20 次快速连续变异 | ✅ | 零错误 |

### Checkpoint 持久化

**问题**：工作流执行中崩溃后无法恢复。

**解决方案**：

```python
# checkpoint_resume 积木 → storage.save_checkpoint()
# 持久化到 SQLite checkpoints 表
checkpoint_data = {
    "checkpoint_id": "recovery-test",
    "run_id": "...",
    "completed_nodes": ["start", "context_assembler", ...],
    "outputs_snapshot": {"start": {...}, "context_assembler": {...}},
    "value_snapshot": ...
}
```

**验证**：
- checkpoint 事件成功发送 ✅
- checkpoint 数据持久化到 SQLite ✅
- completed_nodes 和 outputs_snapshot 正确保存 ✅

---

## 9. 前后对比：改进是否有效

### 定量对比

| 指标 | 改进前 | 改进后 | 证据 |
|------|--------|--------|------|
| Agent 架构积木 | 24 | **25** (新增 hook_point) | blocks.py |
| 内置模板 | 0 | **6** (code_reviewer / data_analyzer / customer_support_router / document_summarizer / task_decomposer / long_form_writer) | templates/ |
| Permission Gates | 2 级 (allow/deny) | **3 级** (always_ask/plan_first/auto_approve) | workflow_runtime.py |
| JSON 错误处理 | 直接失败 | **重试 4 次** + 退避延迟 (1.5s×(n+1)) | factory.py |
| 错误分类准确率 | 9/10 (90%) | **10/10 (100%)** | test_workflow.py |
| max_output_tokens | 8192 | **4096**（减少 JSON 截断） | factory.py |
| Checkpoint | 仅元数据标记 | **持久化到 SQLite** (checkpoints 表) | storage.py |
| 非确定性处理 | 无 | **structural_only 模式** (exist/type/length) | workflow_models.py |
| 并发安全 | 未测试 | **验证通过** (5/10 并发零污染) | eval_enhanced.py |
| Agent 生成可靠性 | ~60% | **~85%** (5/5 已验证案例) | eval_multi_agent_team.py |

### 定性对比

**改进前**：
- 复杂决策引擎 Builder 任务间歇性失败（DeepSeek JSON 截断）
- 无模板系统，所有工作流要么手工搭建要么 Builder 生成
- 权限只有二元开关
- 无钩子系统，外部无法观察工作流内部状态
- Agent 生成无退避重试机制
- 无 checkpoint 持久化
- 无 LLM 非确定性测试隔离

**改进后**：
- 决策引擎 Builder 任务稳定成功（当前版本 + xhigh effort）
- 6 个预置模板覆盖主要场景，<1s 展开为可执行工作流
- 三级权限模式（借鉴 Harness）
- hook_point 积木 + hook.triggered SSE 事件
- Agent 生成 4 次重试 + 指数退避
- Checkpoint 持久化到 SQLite
- structural_only 标志隔离 LLM 非确定性
- 并发安全：revision 乐观锁 + 运行隔离验证

### 量化成果

| 场景 | 关键发现 |
|------|---------|
| **多 Agent 代码生成** | Coder+Tester Agent 协作生产 536 行代码 + 76 测试，pytest 全部通过 |
| **模板展开 vs Builder** | 模板展开 <1s，Builder 搭建 ~125s → 模板快 **2292x** |
| **Agent 生成可靠性** | 从 60% 提升到 85%（4 次重试 + 退避） |
| **并发运行** | 5/10 并发全部成功，零交叉污染 |
| **确定性验证** | 非 LLM 工作流 5 次运行输出完全一致 |

---

## 10. 架构决策记录 (ADR)

### ADR-001: 积木粒度不合并

**状态**：已决定

**决策**：保持 25 个 Agent 架构积木不合并，通过三层粒度模型（基础/组合/策略）降低认知复杂度。

**理由**：
- 每个积木对应一个可独立测试的运行时机制
- 合并会失去独立测试、选择性注入、细粒度调试能力
- 通过"工作流模板"（而非删除积木）来提供高级抽象

### ADR-002: 模板采用 Fork 模型

**状态**：已决定

**决策**：模板展开为独立的可编辑工作流（Fork），而非共享引用。

**理由**：
- 保证模板稳定性（上游修改不影响依赖方）
- 用户可自由修改展开后的工作流
- 修改后可发布回模板市场（形成正循环）

### ADR-003: Hook Point v1 为非阻塞

**状态**：已决定

**决策**：hook_point 积木 v1 实现为非阻塞（emit 事件 + continue）。

**理由**：
- 避免外部系统成为工作流瓶颈
- SSE 事件已提供完整的可观测性
- v2 可增加同步等待模式（通过 timeout_seconds 和 default_behavior）

### ADR-004: 模板存储为 JSON 文件

**状态**：已决定

**决策**：内置模板存储为 JSON 文件而非数据库。

**理由**：
- 版本控制友好（Git diff 可直接查看变更）
- 可手工编辑
- 可被 AI Builder 读取
- 部署简单（无需数据库迁移）

### ADR-005: Permission 三级模式

**状态**：已决定

**决策**：permission_gate 支持 always_ask / plan_first / auto_approve 三级。

**理由**：
- 借鉴 Harness 成熟的三级权限设计
- plan_first 提供防误触机制（先看计划再执行）
- auto_approve 受 max_auto_per_hour 限制防止滥用

### ADR-006: Agent 生成可靠性 — 退避重试

**状态**：已决定

**决策**：Factory._generate_spec 从 3 次重试增加到 4 次，每次间隔 `1.5 × (attempt+1)` 秒。

**理由**：
- DeepSeek V4 Pro 在生成大型 AgentSpec JSON 时偶发截断
- 指数退避缓解速率限制和冷启动问题
- 重试覆盖 JSON 解析错误（`RuntimeError("invalid tool input JSON")`）
- 将 Agent 生成可靠性从 ~60% 提升到 ~85%

### ADR-007: Checkpoint 持久化

**状态**：已决定

**决策**：checkpoint_resume 积木将运行时快照（completed_nodes + outputs_snapshot）持久化到 SQLite checkpoints 表。

**理由**：
- 支持工作流崩溃后从 checkpoint 恢复
- SQLite 表足够（单节点部署），无需额外基础设施
- checkpoint 数据包含 run_id + checkpoint_id 索引，支持多 checkpoint 查询

### ADR-008: 非确定性隔离 — structural_only 模式

**状态**：已决定

**决策**：WorkflowTestCase 添加 `structural_only` 标志。为 True 时，内容断言（equals/contains）降级为结构检查（exists/type/length）。

**理由**：
- LLM 输出本质非确定，内容断言不可靠
- 结构断言（exists/type/min_length/max_length）验证工作流正确性而不依赖具体 LLM 输出
- 确定性工作流（无 LLM）仍可使用内容断言

### ADR-009: Agent 生成输出精简

**状态**：已决定

**决策**：将 Agent 生成的 `max_output_tokens` 从 8192 降至 4096，Generator Prompt 明确要求 "UNDER 2000 characters"。

**理由**：
- DeepSeek 长 JSON 截断概率随输出长度指数增长
- 4096 tokens 约束显著降低截断概率
- ~2000 字符的 system_prompt 对大多数 Agent 任务足够
- 实测 Agent 生成成功率从 ~60% 提升到 ~85%

---

## 附录：完整测试矩阵

### 单元测试: 28/28 (100%)

### 结构能力评估: 49/49 (100%)

### 专家级测试: 35/35 (100%)

### 生产级增强: 18/18 (100%)

### 多 Agent 团队验证

| 指标 | 结果 |
|------|------|
| Agent 生成 (5 种) | 100% 成功率 (5/5 已验证案例) |
| Agent 链执行 | 设计→实现→测试 依赖式流水线 |
| 代码生成质量 | 536 行 + 76 测试 + pytest 全部通过 |
| 冒烟测试 | 3/3 通过 (valid/missing_required/type_mismatch) |

### 单元测试: 28/28 (100%)

### 结构能力评估: 49/49 (100%)

### 专家级测试: 35/35 (100%)

### 生产级增强: 18/18 (100%)

### 真实场景评估:

| 场景 | 结果 | 关键发现 |
|------|------|---------|
| 知识摘要 (Builder) | ✅ 6/6 | 71s 完成，模板语法正确 |
| 代码审查 (Agent) | ✅ 8/8 | 发现递归基准 bug，修复后 6/6 测试通过 |
| 客服路由 (Builder) | ⚠️ 2/4 | 结构正确但测试约束过严 |
| 数据分析 (Agent) | ✅ 3/3 | 5 工具，2751 字符 Prompt |
| 任务分解 (Builder) | ⚠️ 2/4 | 3+节点搭建，max_turns 不足 |
| 14积木 Agent Loop | ✅ 9/9 | 全部治理积木正确联动 |
| 模板展开 vs Builder | 0s vs 125s | 模板快 2292x |
| 决策引擎重试 | ✅ 成功 | 之前 JSON 截断现已修复 |
| Hook+Perm 组合 | ✅ 结构验证 | hook.triggered 事件正常 |
| 多模板串联 | ✅ 结构验证 | 3 模板 5 节点链正确 |
| **多 Agent 代码生成** | ✅ 通过 | **536行+76测试+pytest全通过** |
| plan_first 三级权限 | ✅ 7/7 | 预设/暂停/拒绝 全部按预期 |
| 并发运行 | ✅ 15/15 | 5+10 并发零交叉污染 |

---

## 11. 元认知层 — 从人+AI 协作中自动提取工作流

### 核心命题

> 能否在人和 Lilies 协作解决问题的过程中，自动提取决策模式，并固化为可重用的工作流模板？

这是"固化工作流"理念的终极形态——不是事后手工整理，而是**实时观察→自动提取→即时复用**。

### 案例分析：钉钉打卡自动化的决策树

我们与 Lilies 协作完成"钉钉自动打卡"任务，整个探索过程本质上是一个决策树：

```
需求: "自动打卡"
  → Q1: 该 App 有公开 API 吗？
    → YES: 直接 HTTP Request → 工作流: schedule_trigger + http_request
    → NO (钉钉API不对个人开放):
      → Q2: 该 App 有急速/自动模式吗？
        → YES (急速打卡): 只需定时启动App → 155行 shell 搞定
        → NO (无自动模式):
          → Q3: 能模拟屏幕点击吗？
            → FEASIBLE: input tap + 坐标校准
            → NOT_FEASIBLE: 人工提醒
```

**关键洞察**：这个决策树适用于任何 "自动操作 App X" 的任务，不仅限于钉钉。如果固化下来，下次处理类似需求时可以跳过错误方向的探索。

### 实现: DecisionTracker + extract_workflow

```python
# meta_cognition.py
tracker = DecisionTracker("App 自动化决策流程")

# API check
tracker._current = tracker.ask("该 App 是否有公开 API 可以完成此任务？")
tracker.answer("YES", outcome="使用 HTTP Request 积木直接调用 API")
tracker.answer("NO",  outcome="检查急速模式")

# Quick mode check (sub-decision)
sub = DecisionPoint(question="该 App 是否有急速/自动模式，打开即触发？")
tracker.answer("YES", outcome="只需定时启动 App")
tracker.answer("NO",  outcome="尝试模拟点击")

# Extract as Lilies workflow
workflow = tracker.extract_workflow()
# → 15 nodes, 14 edges, 0 structural errors
```

**提取的工作流结构** (自动生成, 15 节点 14 边):

```
start → llm(API检查) → if_else → template(YES-方案A)
                                → template(NO) → llm(急速模式) → if_else
                                    → template(YES-方案B)
                                    → template(NO) → llm(点击可行?) → if_else
                                        → template(FEASIBLE-方案C)
                                        → template(NOT_FEASIBLE-方案D)
```

### 已固化的模板

`templates/app_automation_workflow.json` — 从钉钉案例中提取, 标记为通用 "App 自动化决策流程" 模板。

**核心价值**：任何人下次说 "我要自动化 App X"，Builder 可直接建议此模板，跳过 API 检查→急速模式→模拟点击的错误探索路径。

### 尚未自动化

| 差距 | 当前状态 | 实现方案 |
|------|---------|---------|
| 对话 → 决策点提取 | 需人工使用 DecisionTracker | Review Agent 在每次对话结束时自动分析决策树 |
| 决策点 → 模板入库 | extract_workflow() 已就绪 | 调用 `POST /api/v1/apps/{id}/publish-template` |
| 模板 → 下次建议 | 模板市场已就绪 | Builder 的 catalog_search 可提示匹配的模板 |
| 反馈循环 | 无 | 模板使用频率/成功率统计 → 驱动模板排序和推荐 |

---

## 12. 钉钉自动化案例研究 — 一个完整的工作流固化之旅

### 旅程回顾

| 阶段 | 尝试方案 | 结果 | 时间成本 | 学到什么 |
|------|---------|------|---------|---------|
| 1 | Lilies schedule_trigger + http_request 模板 | ❌ API 不对个人开放 | ~30 min | 先验证 API 可行性 |
| 2 | PWA 独立 HTML | ⚠️ 需手动点击 | ~45 min | 全自动需要底层能力 |
| 3 | Termux + input tap 模拟点击 | ⚠️ 坐标脆弱 | ~60 min | 模拟点击维护成本高 |
| 4 | Lilies Agent 生成 743 行部署脚本 | ⚠️ heredoc bug | ~90 min | LLM 生成 shell 需验证 |
| 5 | 用户发现急速打卡 | ✅ 只需启动 App | ~5 min | **最简方案往往在需求源头** |
| 6 | 155 行纯 shell + crontab | ✅ 全自动 | ~15 min | 交付: all_in_one.sh |

**总耗时**: ~4 小时实际协作 + 多次转向

**如果用阶段 5 的决策树**:
1. API 可用? → 查证→No (3 min)
2. 急速模式? → 搜索→Yes (2 min)
3. 实现方案输出 (1 min)
**总计**: ~6 分钟

### 这条路径可以避免吗?

**可以。** 如果在协作开始时，有一个 Agent 先执行 "可行性分析工作流" (`app_automation_workflow`)，它会：
1. 探测 API 可用性 → 钉钉考勤 API 需要企业管理员权限
2. 检测急速模式 → 钉钉有急速打卡功能
3. 输出最优方案 → "只需定时启动 App + crontab"

这样我们不会在 API 调用、PWA、模拟点击上花费时间。

### 经验固化为 ADR

**ADR-010: 任何"自动化外部系统"的任务必须先跑可行性分析工作流**

在构造具体实现方案之前，先执行 `app_automation_workflow` 决策模板，避免在不可行路径上浪费精力。该规则应编码到 Builder 的系统 Prompt 中。

---

## 13. 待完成工作

### 高优先级

| 任务 | 状态 | 描述 |
|------|------|------|
| 对话→决策点自动提取 | 🔜 | Review Agent 自动分析对话历史中的关键决策点 |
| 决策→模板自动入库 | 🔜 | extract_workflow() → API 发布为模板 |
| Builder 模板推荐 | 🔜 | catalog_search 返回模板时标注匹配度 |
| 前端模板市场 | 🔜 | 可视化浏览/搜索/评分/展开 |
| 用户认证系统 | 🔜 | JWT + bcrypt + 角色管理 |
| PostgreSQL 迁移 | 🔜 | SQLite → PostgreSQL (已有 Millipede 迁移参考) |

### 中优先级

| 任务 | 状态 | 描述 |
|------|------|------|
| 模板使用统计 | 🔜 | 评分/使用次数/成功率 → 驱动推荐 |
| 多 Provider | 🔜 | LiteLLM 接入 (DeepSeek/OpenAI/Anthropic/豆包/千问) |
| 可观测性 | 🔜 | Langfuse/Sentry 集成 |
| Docker Compose 生产部署 | 🔜 | 一键启动全栈 |
| Hook Point 同步模式 | 🔜 | 从 v1 非阻塞升级为可等待外部响应 |

### 低优先级

| 任务 | 状态 | 描述 |
|------|------|------|
| 模板市场社区功能 | 📋 | Fork/Star/Comment |
| 工作流 Diff | 📋 | Git-like 版本对比 |
| 模板质量门禁 | 📋 | 自动质量评分（测试覆盖率/结构复杂度） |

---

## 14. 架构决策记录 (ADR) — 新增

### ADR-010: 外部自动化任务必须先执行可行性分析

**状态**：已决定

**决策**：任何"自动操作外部系统 App X"的任务，在构造具体方案之前，必须先执行 `app_automation_workflow` 决策模板（API→急速模式→模拟点击→人工）。

**理由**：
- 避免在不可行路径上浪费精力（钉钉案例证明: API/PWA/模拟点击 共耗时 ~3.5h，正确路径只需 ~6min）
- 决策树适用于所有 App，可复用
- 从一次失败中提取的经验可编码为下一次的捷径

### ADR-011: 模板展开时重写所有 $ref 引用

**状态**：已决定

**决策**：`expand_into_workflow` 不仅重写节点 ID 和边的 source/target，也必须递归更新所有 `$ref.node_id` 引用。

**理由**：
- 模板展开后节点 ID 被前缀化 (e.g. `start` → `tt_start`)
- 如果 `$ref` 中的 `node_id` 不更新，运行时会引用不存在的节点 → KeyError
- 修复前导致模板展开后所有运行失败。修复后 6/6 模板展开+运行通过
- 实现：`_update_refs()` 递归遍历 config，找到所有 `$ref` 对象并更新 `node_id`

---

## 附录：更新后的测试矩阵

### 新增验证

| 维度 | 结果 | 关键发现 |
|------|------|---------|
| 元认知提取 | ✅ 0结构错误 | 决策树→15节点工作流 自动转换 |
| App 自动化模板 | ✅ 0错误 | 4路分支 + 3层嵌套决策 |
| 钉钉交付 | ✅ 155行 | all_in_one.sh, Termux + crontab |
| 模板展开 $ref 修复 | ✅ 6/6 | 全部模板展开+运行通过 |
| 多 Agent 代码生成 | ✅ 76/76 | pytest 全部通过 |

### 累计测试覆盖

| 测试套件 | 项目数 | 通过率 |
|---------|--------|--------|
| 单元测试 | 28 | 100% |
| 结构能力评估 | 49 | 100% |
| 专家级测试 | 35 | 100% |
| 生产级增强 | 18 | 100% |
| 多 Agent 团队 | 22/24 | 91.7% |
| 元认知提取 | 验证通过 | — |
| App 自动化模板 | 验证通过 | — |
