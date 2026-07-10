# E08 Harness Sidecar Passmode Ledger

状态：followup_controls_scope_selected；editable policy-controls API next

## 当前结论

v0.2.3-v0.2.28 已推进多项 Platform Harness slice：task monitor、observability UI、durable storage、owner budget、stale reconciliation、secret policy、HTTP/stdio egress、worker lease/runner/CLI/renewal 等。它们是重要工程前置，但不等于 E08 sidecar/passmode 对照实验完成。

v0.2.55 补上了第一轮 runnable deterministic comparison：`permission_gate` 的 `always_ask` 会让 workflow-internal soft gate 暂停，`auto_approve` 会让同类 soft gate 通过，而 Platform Harness network egress policy 会在外部动作前 hard-fail run。结论是：workflow-internal passmode 适合表达产品可见的暂停/恢复/审阅语义；Platform Harness sidecar 才是不可由 workflow 配置绕过的硬边界。

## 证据

| 项目 | 路径 |
| --- | --- |
| 工程证据审查报告 | `../reports/2026-07-09_1809_E08_harness_sidecar_passmode_evidence_review.docx` |
| 首轮 deterministic 对照报告 | `../reports/2026-07-10_0755_E08_harness_sidecar_passmode_comparison.docx` |
| 首轮 raw evidence | `../evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10_summary.md` |
| 相关 stage chain | `../../stage-reports/v0.2.3_platform_harness_and_development_roadmap.md` 到 `../../stage-reports/v0.2.28_worker_heartbeat_and_renewal.md` |
| 最新 timeout/build-deadline 应用 | `../../stage-reports/v0.2.40_builder_provider_timeout_boundary.md`、`../../stage-reports/v0.2.42_builder_build_level_watchdog.md` |

## 边界

当前 Platform Harness 仍有未闭环方向：allowlist-grade stdio/container egress、KMS/rotation、完整 handler catalog、分布式 heartbeat registry、policy controls 完整 UI/API、长时间运行 runbook。E08 首轮对照不等于完整 sidecar 产品实现。

## 下一步

扩展 E08 对照到 cancellation、budget、worker lease、UI/API controls，或将本轮结论沉淀为 Harness language/current design 规则。

v0.2.57 final disposition：Workflow-internal passmode can pause/pass by config; Platform Harness sidecar hard-blocks before external action. Extended controls remain product follow-up.

证据：`../evidence/experiment_v0.2.57_full_backlog_closure_2026_07_10_summary.md`

v0.2.94 productization lane reselection 已选择 E08 follow-up controls 作为下一条 P1 lane。E07 guarded default rollout 已完成，E02/E10 存在外部/治理阻塞，E05 monitoring 已有 completed slice，因此 E08 是最高优先级且未阻塞的剩余产品化 gap。

证据：`../../workingon-archives/v0.2.94/decision_v0.2.94_productization_lane_reselection_summary.md`

v0.2.95 E08 follow-up controls scope 已选择 `editable_policy_controls_api` 作为下一版具体实现切片。选择理由是：v0.2.65-v0.2.66 已有只读 policy-controls 与行为矩阵，v0.2.68 已有 cancellation/budget 行为证据，v0.2.20-v0.2.28 已有 worker lease 后端/runner/续租证据；继续重复这些证据不会推进产品化。下一步应先建立受审计的后端 mutation contract，再进入 Studio 可编辑 UI。

证据：`../../workingon-archives/v0.2.95/scope_v0.2.95_e08_followup_controls_summary.md`
