# E07 Complexity Router Ledger

状态：completed_as_policy_hypothesis；not enabled as default

## 实验问题

能否根据需求难度自动选择 operator、workflow depth、模型、Builder Team 或 skill 链条，使简单任务低成本完成，复杂任务获得更长计划和更强模型支持？

## 当前证据

v0.2.57 deterministic router fixture 已完成：simple/medium/complex routing hypotheses 已基于 E01/E05 evidence 显式化，但 `router_ready_for_default=false`。

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`

## 初始设计方向

- 定义 simple/medium/complex 三档需求。
- 为每档指定 builder policy：max turns、repair cycles、reuse depth、model tier、是否 plan-first。
- 比较 routing 前后的成功率、成本、超时率和人工修复率。

## 下一步

后续若要产品化，需要 guardrails 和 rollout design；当前假设不得直接写入默认策略。
