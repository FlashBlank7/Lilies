# E08 Harness Sidecar Passmode Ledger

状态：current tranche productized without full sidecar completion；paused for lane reselection

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

v0.2.96 已实现 `PATCH /api/v1/platform/harness/policy-controls`，形成受审计的后端 mutation contract。该接口可更新 network egress policy/allowlist、cancellation policy、secret policy、worker lease seconds 和 task/owner budget limits，返回 before/after/audit，并拒绝空 patch、空 host 与负 limit。`cancellation_policy=disabled` 会实际阻止 workflow run cancel endpoint。此版本仍不声明 full sidecar completion，也不实现 Studio 可编辑 UI。

证据：`../../workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md`

v0.2.97 已在后端 API 完成后选择 `studio_editable_policy_controls` 作为下一条 E08 产品化路径。operator runbook 延后到 operator surface 之后；broader sidecar boundary closure 因范围过大延后；暂停 E08 被拒绝，因为 API 尚未进入操作员可用表面。

证据：`../../workingon-archives/v0.2.97/decision_v0.2.97_e08_post_api_productization_summary.md`

v0.2.98 已将 editable policy-controls 暴露到 Studio monitor tab：包含 network policy、allowlist、cancellation policy、secret policy、worker lease、budget limits、reason 和 save action，调用 `PATCH /api/v1/platform/harness/policy-controls`，并保留 before/after/audit 后端证据链。前端 TypeScript、route smoke、backend focused regression 均通过。此版本仍不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md`

v0.2.99 已选择 `operator_runbook_lifecycle` 作为 Studio controls 之后的下一条 E08 产品化路径。broader sidecar boundary closure 仍因范围过大延后；暂停 E08 被拒绝，因为已有 API 与 Studio controls 后，操作员流程、回滚和升级边界是自然闭合步骤。

证据：`../../workingon-archives/v0.2.99/decision_v0.2.99_e08_post_studio_controls_summary.md`

v0.2.100 已新增稳定 runbook `docs/operator-runbooks/e08_policy_controls_operator_runbook.md`，覆盖 before-change checks、apply-change procedure、post-change verification、rollback、incident escalation 和 evidence checklist，并链接 v0.2.96 backend API 与 v0.2.98 Studio controls 证据。验证脚本确认必需章节、关键 API/UI 证据链接和 full-sidecar 边界声明均存在。broader sidecar boundary closure 仍未完成。

证据：`../../workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md`

v0.2.101 已决定暂停当前 E08 tranche 并回到 productization lane reselection。当前 E08 tranche 已完成 deterministic comparison、editable backend API、Studio editable controls、operator runbook lifecycle；但 broader sidecar boundary closure 仍延后，不能写成 full sidecar completion。

证据：`../../workingon-archives/v0.2.101/decision_v0.2.101_e08_post_runbook_disposition_summary.md`

v0.2.102 productization lane reselection 未选择 E08 broader sidecar boundary closure 作为下一版实现切片。该方向保持未完成/延后状态，因为 full sidecar closure 范围过宽，不能由当前 E08 tranche 的 API、Studio controls 和 runbook 证据冒认。下一条 lane 选择为 E05 scheduled monitoring hook。

证据：`../../workingon-archives/v0.2.102/decision_v0.2.102_productization_lane_reselection_summary.md`

v0.2.104 productization lane reselection 已在排除 completed E05 scheduled hook、completed E07 guarded rollout 以及 blocked E02/E10 后，选择 `e08_broader_sidecar_scope_decomposition` 作为下一条 open lane。该选择只表示下一版要做 E08 broader sidecar scope decomposition，不表示 full Platform Harness sidecar 已完成。

证据：`../../workingon-archives/v0.2.104/decision_v0.2.104_productization_lane_reselection_summary.md`

v0.2.105 E08 broader sidecar scope decomposition 已把 remaining full-sidecar gap 拆为 concrete slices，并选择 `stdio_container_egress_allowlist_contract` 作为第一条实现切片。当前 tranche 的 deterministic comparison、editable policy-controls API、Studio controls、operator runbook 已映射为 completed capabilities，不会重复实现；full sidecar completion 仍未声明完成。

证据：`../../workingon-archives/v0.2.105/scope_v0.2.105_e08_broader_sidecar_decomposition_summary.md`

v0.2.106 已实现 allowlist-grade stdio/container egress contract：`MCPServerSpec` 可声明 `egress_hosts`，Platform Harness 只允许 sandboxed stdio allowlist 且 declared hosts 被 agent/platform allowlist 覆盖的场景；missing hosts、unlisted hosts、unsandboxed allowlist stdio 会在外部动作前 hard-fail。Policy controls 现在展示 allowlist contract requirements。该版本只关闭 stdio/container egress allowlist slice，不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md`
