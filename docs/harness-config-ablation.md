# Harness 配置分层与消融实验设计表

> 目标：把构建 harness 的每条纪律从"写死"变成"显式开关"，同一套机制同时服务两件事——
> UI 高级配置面板（用户可见、可调）与论文消融实验（每条防线可独立关闭、效果可测量）。
> 每次构建的完整配置随 build 记录落库，构成可复现的配置指纹。

## 分层现状

| 层 | 内容 | 状态 |
| --- | --- | --- |
| 第一层 | `BuildRequest` 已有的 5 个参数 | ✅ 已完成（2026-08-18 前端三入口暴露） |
| 第二层 | builder 模块级写死常量 → `BuildRequest` 可选字段 | 待改造，本文档列清单 |
| 第三层 | 诚实失败防线 → `defenses` 配置对象 | 待改造，论文消融的核心 |

## 第一层（已完成）：BuildRequest 参数 + 前端面板

参数定义在 `workflow_models.py:305-311`，2026-08-18 起由共享组件
`app/components/BuildAdvancedConfig.tsx` 在三个构建入口统一暴露：

| 入口 | 位置 | 此前行为 | 现在 |
| --- | --- | --- | --- |
| 首页创建 | `app/page.tsx` | 硬编码 36/4/480/auto_publish=true | 面板可调，默认自动发布 |
| 会话页首建 | `app/applications/[id]/session/page.tsx` | 只传 auto_publish=false，其余吃后端默认 | 面板可调，默认手动发布 |
| 画布详情页 | `app/applications/[id]/page.tsx` | 仅一个 deadline 输入框 | 完整面板（深色调） |

面板折叠时 summary 常驻一行配置摘要（"36 轮 · 修复 4 · 480 秒 · 规划自动 · 自动发布"），
让每次构建的预算不再隐形。

**顺带修复的语义缺陷**：详情页 deadline 帮助文案称"留空 = 不设超时"，但旧代码留空时
省略字段，后端默认 480 秒仍然生效。现在留空显式发送 `null`，文案与行为一致。

| 参数 | 默认 | 范围 | 消融观测点 |
| --- | --- | --- | --- |
| `max_turns` | 36 | 5–200 | 回合预算 vs 交付率曲线 |
| `max_repair_cycles` | 4 | 1–30 | 修复预算 vs 原地打转率 |
| `max_elapsed_seconds` | 480 | 0.001–86400 或 null | 时限压力 vs 交付质量 |
| `planning_mode` | auto | auto/required/disabled | 强制规划对复杂需求一次通过率的影响 |
| `auto_publish` | true | bool | （交付流程开关，非实验变量） |
| `builder` | classic | 注册表内引擎名 | **实验主维度**：单大模型 vs 小模型集群等多套 builder 对照（2026-08-18 落地，`builder_registry.py`；随 build 记录落库，属配置指纹一部分） |

## 第二层：写死常量 → BuildRequest 可选字段

改造路径统一：`BuildRequest` 新增可选字段 → `api.py:create_build` 透传 →
`workflow_storage.create_build` 落库 → `builder.py` 从 build 记录读取（替代模块级常量）。

| 常量 | 位置 | 现值 | 语义 | 消融假设 |
| --- | --- | --- | --- | --- |
| `BUILDER_MAX_STALLED_PROGRESS_TURNS` | `builder.py:242` | 6 | 连续无耐久进展 N 轮处决构建 | 放宽是否救回慢热型构建 / 收紧是否省成本 |
| `BUILDER_MAX_DISCOVERY_ONLY_TURNS` | `builder.py:243` | 10 | 连续纯探索 N 轮处决构建 | 探索预算与需求复杂度的匹配 |
| `BUILDER_TEAMMATE_MAX_TURNS` | `builder.py:244` | 8 | 队友单次任务回合上限 | 委派深度 vs 协调开销 |
| `BUILDER_TEAMMATE_FOLLOWUP_MAX_TURNS` | `builder.py:303` | 6 | 队友追问回合上限 | 同上 |
| `TEAMMATE_MIN_REMAINING_SECONDS` | `builder.py:240` | 90.0 | 剩余时间不足时禁止再派队友 | 尾段委派的边际价值 |
| `TOOL_RESULT_HISTORY_MAX_CHARS` | `builder.py:238` | 6000 | 历史工具结果截断长度 | 上下文瘦身 vs 信息损失 |
| `TOOL_RESULT_KEEP_RECENT_TURNS` | `builder.py:239` | 8 | 最近 N 轮工具结果不截断 | 同上 |
| `max_output_tokens`（每轮） | `builder.py:944` | 8192 | 单轮输出上限 | 截断自愈触发频率 |
| 插话收件箱上限 | `builder.py:417` | 20 条 / 8000 字 | 业主实时插话缓冲 | （工程参数，非实验变量） |

注意：第二层字段应标记为**专家参数**，普通入口不露出（见第三层的暴露策略）。

## 第三层：诚实失败防线 → `defenses` 配置对象

设计：`BuildRequest` 新增 `defenses: dict[str, bool] | None`（None = 全开，即今天的行为），
落库进 build 记录，`builder.py` / 运行时在各执法点读取。**生产入口不暴露此对象；
仅实验脚本与专家/调试模式可用。**

### 已核实的硬门（边界强制，模型无关）

| 防线 key（建议） | 机制 | 执法点 | 消融观测指标 |
| --- | --- | --- | --- |
| `leak_gate_ask_owner` | 术语泄漏扫描拒绝 ask_owner | `builder.py:1232`（词表 `builder.py:392-408`） | 客户可见文本泄漏率 |
| `mandatory_smoke_test` | 无测试的草稿自动补强制冒烟测试 | `builder.py:670` | 发布后首跑失败率 |
| `manual_lookup_gate` | 架构积木先查手册才能使用 | `builder.py:1594-1595` | 架构积木误配率 |
| `node_removal_test_guard` | 删节点不得破坏测试依赖 | `builder.py:1606`（实现 `builder.py:785`） | 拆图调试事故率 |
| `repair_cycle_gate` | 同一失败 revision 修复超限封锁 test_run | `builder.py:1653-1662` | 原地打转轮数、成本 |
| `stalled_progress_guard` | 无耐久进展处决 | `builder.py:1114-1124` | 空转成本、误杀率 |
| `discovery_only_guard` | 纯探索超限处决 | `builder.py:1125-1135` | 同上 |
| `teammate_guard` | 修复耗尽/时间不足禁止委派 | `builder.py:2153-2177` | 尾段委派浪费 |
| `planning_required_gate` | required 模式下未出计划禁改草稿 | `builder.py:1937-1939` | 复杂需求返工率 |

### 已核实的运行时防线

| 防线 key（建议） | 机制 | 执法点 | 消融观测指标 |
| --- | --- | --- | --- |
| `sum_all_miss_sentinel` | `$sum` 过滤条件全部取不到字段时报错而非归零 | `workflow_runtime.py:4836`（注释 `:4876`，ERP 盲测真凶） | 静默全零率 |
| `unknown_config_key_error` | 积木配置未知键即报错（不静默忽略） | `blocks.py:1995` | 配置"看着对"事故率 |

### 提示词级软约束（消融 = 从系统提示词删除该条）

| 防线 key（建议） | 提示词条目 | 消融观测指标 |
| --- | --- | --- |
| `prompt_number_anchoring` | 数值锚定测试（equals 断言精确数字） | 静默失败率（发布后审计） |
| `prompt_empty_result_case` | 外部数据流强制空结果用例 | 空上游造假率 |
| `prompt_assertion_freeze` | 断言一经执行冻结、不得弱化 | 验收弱化次数 |
| `prompt_safety_not_contains` | 客服流强制不安全补救 not_contains 断言 | 安全违规输出率 |
| `prompt_deterministic_blocks` | 确定性工作禁入 LLM 提示词 | 模型做算术的审计发现数 |

### 已知缺口（论文 limitations 或补齐后成为贡献）

| 缺口 | 说明 |
| --- | --- |
| 叙述/交付说明无泄漏硬门 | `_internal_terms_in` 只挂在 ask_owner；模型的过程叙述和最终交付说明不经过它。补一个出口硬门即是新防线 `leak_gate_narration` |
| ask_owner 次数无计数器 | "最多两次"纯靠提示词 |
| 断言弱化无检测 | `test_add` 同 id 替换合法，不比对新旧断言强度 |
| "模板回声检测" 落点待盘点 | README 九防线之一，本次未在代码中定位到执法点，写论文前需确认实现位置或承认为提示词级 |

## 消融实验骨架

- **任务集**：从 5 个真实项目（珠宝/法律/电梯/ERP 日报/促销预测）泛化 20–50 个变体任务。
- **臂**：全开（对照）× 逐防线单关 × 关键组合（如同时关数值锚定+空结果用例）。
- **每臂**：≥3 seed × ≥2 模型（验证"harness 与模型无关"主张，含一个小模型）。
- **主指标**：一次通过率、静默失败率（发布后独立审计）、修复循环数、单构建成本、泄漏率。
- **可复现性**：build 记录已存 max_turns 等参数（`workflow_storage.create_build`）；
  `defenses` 与第二层字段入库后，配置指纹即完整。禁用防线的构建在 UI 上应有醒目标记，
  防止实验配置流入真实交付。
