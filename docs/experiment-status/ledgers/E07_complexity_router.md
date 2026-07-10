# E07 Complexity Router Ledger

状态：limited_default_readiness_passed；next guarded default rollout；default settings still disabled

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

v0.2.85 post-operator-opt-in decision 已完成：选择 `repair_frontend_verification_environment`。stage_0 和 stage_1 rollout evidence 已满足，但 `node=false`、`npm=false`，因此默认启用评审继续推迟。

证据：

- `../../workingon-archives/v0.2.85/decision_v0.2.85_complexity_router_post_operator_opt_in_summary.md`

v0.2.86 frontend verification environment repair 已完成：repo-local runner 发现 `/Users/zhonghaoyang/.nvm/versions/node/v24.15.0/bin`，并成功执行 `npm run lint` 与 `node_modules/.bin/tsc --noEmit`，两个命令 return code 均为 `0`。

证据：

- `../../workingon-archives/v0.2.86/verification_v0.2.86_frontend_environment_repair_summary.md`

v0.2.87 default enablement review decision 已完成：所有 gate 均通过，选择 `enter_default_enablement_review`，下一步进入 limited default enablement plan。当前版本仍保持 `default_enabled=false`。

证据：

- `../../workingon-archives/v0.2.87/decision_v0.2.87_complexity_router_default_enablement_review_summary.md`

v0.2.88 limited default enablement plan 已完成：定义 limited default scope、config/API contract、rollback triggers 和 verification gates。计划版本不启用默认，`implementation_in_this_version=false`，`default_enabled=false`。

证据：

- `../../workingon-archives/v0.2.88/plan_v0.2.88_complexity_router_limited_default_enablement_summary.md`

v0.2.89 limited default enablement contract 已完成：新增 backend settings、API/status surface、classification response fields 和 rollback-to-disabled status。默认 settings 仍为 `disabled` 且 `default_enabled=false`；显式配置 `limited_default + enabled` 时，eligible classification 可暴露 `default_builder_policy`；unknown 仍保持 complex-equivalent 且不启用 default router；frontend verification 通过。

证据：

- `../../workingon-archives/v0.2.89/contract_v0.2.89_complexity_router_limited_default_enablement_summary.md`
- `../../workingon-archives/v0.2.89/implementation_v0.2.89_complexity_router_limited_default_enablement_contract.md`

v0.2.90 runtime activation path 已完成：创建 build 时会基于 settings 执行 complexity-router activation。默认 settings 不激活 runtime builder policy；显式 `limited_default + enabled` 的 simple build 会持久化 runtime builder policy、把 auto planning 解析为 `disabled`，并让 Builder omitted `template_suggestions` 使用 `reuse_depth=shallow`、`reuse_depth_source=complexity_router`。unknown 仍不激活 runtime policy，frontend verification 通过。

证据：

- `../../workingon-archives/v0.2.90/activation_v0.2.90_complexity_router_runtime_activation_path_summary.md`
- `../../workingon-archives/v0.2.90/implementation_v0.2.90_complexity_router_runtime_activation_path.md`

v0.2.91 runtime activation observability 已完成：新增只读 metrics surface，可统计 active、bypassed、disabled-default、conservative-unknown、request-override，并暴露 classification distribution、effective planning mode distribution、runtime reuse-depth distribution、build outcome 和 sampled records。默认 metrics 记录 `active=0`、`disabled_default=1`；显式 limited-default metrics 记录 `active=2`、`bypassed=1`、`conservative_unknown=1`、`request_override=1`，frontend verification 通过。

证据：

- `../../workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md`
- `../../workingon-archives/v0.2.91/implementation_v0.2.91_complexity_router_runtime_activation_observability.md`

v0.2.92 limited-default product readiness review 已完成：7/7 gates 通过，选择 `enter_guarded_default_rollout`，下一版本为 `v0.2.93_complexity_router_guarded_default_rollout`。本 readiness stage 不改变正常默认 settings，仍为 `disabled`；guarded rollout 必须继续保留 rollback value `disabled` 和 conservative unknown bypass。

证据：

- `../../workingon-archives/v0.2.92/decision_v0.2.92_complexity_router_limited_default_readiness_review_summary.md`
- `../../workingon-archives/v0.2.92/implementation_v0.2.92_complexity_router_limited_default_readiness_review.md`

## 初始设计方向

- 定义 simple/medium/complex 三档需求。
- 为每档指定 builder policy：max turns、repair cycles、reuse depth、model tier、是否 plan-first。
- 比较 routing 前后的成功率、成本、超时率和人工修复率。

## 下一步

下一步是实现 guarded default rollout。默认 settings 是否变更只能由 v0.2.93 的 source-linked design 和验证决定；rollback value 必须保持 `disabled`，unknown 必须继续 bypass。
