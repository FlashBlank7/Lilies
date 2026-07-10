# E07 Complexity Router Ledger

状态：operator_opt_in_rollout_completed；not enabled as default

## 实验问题

能否根据需求难度自动选择 operator、workflow depth、模型、Builder Team 或 skill 链条，使简单任务低成本完成，复杂任务获得更长计划和更强模型支持？

## 当前证据

v0.2.57 deterministic router fixture 已完成：simple/medium/complex routing hypotheses 已基于 E01/E05 evidence 显式化，但 `router_ready_for_default=false`。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`

v0.2.70-v0.2.74 productization guardrails 已完成：default-safety gate、requirement classification contract、operator override plan、rollout metrics prerequisites 均已具备 API / tests / evidence；`allowed_to_enable_default=true` 只表示进入启用评审的前置条件满足，不表示默认行为已启用。

证据：

- `../../stage-reports/v0.2.74_complexity_router_rollout_metrics_prerequisites.md`
- `../../workingon-archives/v0.2.74/metrics_v0.2.74_complexity_router_summary.md`

v0.2.78 bounded live validation 已完成：三条 v0.2.76 validation cases（simple / medium / complex）均由 DeepSeek live provider 分类通过，`default_enabled=false` 保持不变。

证据：

- `../../workingon-archives/v0.2.78/live_v0.2.78_complexity_router_bounded_validation_summary.md`

v0.2.82 shadow-only rollout 已完成：复用三条 v0.2.76 validation cases，stage_0 shadow-only 本地分类全部匹配 expected class，classification distribution 已记录，unexpected classification rate `0.0`，accidental default enablement count `0`，`default_enabled=false` 保持不变。

证据：

- `../../workingon-archives/v0.2.82/rollout_v0.2.82_complexity_router_shadow_only_summary.md`

v0.2.83 post-shadow rollout decision 已完成：选择 `execute_operator_opt_in_rollout`，拒绝继续原地 shadow-only 和直接进入默认启用评审。原因是 stage_0 已满足退出标准，而默认启用仍缺 operator opt-in evidence 和 frontend verification。

证据：

- `../../workingon-archives/v0.2.83/decision_v0.2.83_complexity_router_post_shadow_rollout_summary.md`

v0.2.84 operator opt-in rollout 已完成：三条 validation cases 均通过 operator opt-in override，override reason coverage `1.0`，unexpected classification rate `0.0`，accidental default enablement count `0`，`default_enabled=false` 保持不变。

证据：

- `../../workingon-archives/v0.2.84/rollout_v0.2.84_complexity_router_operator_opt_in_summary.md`

## 初始设计方向

- 定义 simple/medium/complex 三档需求。
- 为每档指定 builder policy：max turns、repair cycles、reuse depth、model tier、是否 plan-first。
- 比较 routing 前后的成功率、成本、超时率和人工修复率。

## 下一步

下一步需要显式决定 post-operator-opt-in 路径：默认启用评审、继续 opt-in 观测，或先关闭 frontend verification blocker。默认行为仍不得自动启用。
