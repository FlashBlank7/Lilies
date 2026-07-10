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

v0.2.107 remaining sidecar slice reselection 已排除 completed stdio/container egress allowlist slice，并选择 `secret_kms_rotation_contract` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 KMS/rotation，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.107/decision_v0.2.107_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.108 已实现 local KMS/rotation-grade secret envelope contract：新 secret 使用 `secret-envelope:v2:` 与 `key_id`，Platform Harness 支持 current key id 和 previous-key keyring 解密，v1 envelope 与 legacy plaintext rows 继续可读，public metadata 只暴露 storage mode/key id 且不泄露 secret value。该版本只关闭 local secret envelope rotation slice；external KMS、complete handler catalog、distributed heartbeat registry 和 full sidecar completion 仍未完成。

证据：`../../workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md`

v0.2.109 remaining sidecar slice reselection 已排除 completed stdio/container egress allowlist slice、completed secret KMS/rotation slice、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `complete_handler_catalog` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 handler catalog，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.109/decision_v0.2.109_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.110 已实现 complete handler catalog coverage：所有 Platform Harness `TaskKind` 都有 worker handler catalog entry，`scheduler_manual_trigger` 仍是当前唯一真实实现的 worker-owned handler，其他 task kinds 通过 deterministic unavailable handler 明确失败并给出 operator action。新增 `GET /api/v1/platform/harness/worker-handler-catalog` 暴露 catalog/registry/full-execution coverage。该版本只关闭 handler catalog visibility 和 deterministic gap failure slice，不声明 full execution coverage 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md`

v0.2.111 remaining sidecar slice reselection 已排除 completed handler catalog、stdio/container egress、secret rotation、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `distributed_heartbeat_registry` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 distributed heartbeat registry，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.111/decision_v0.2.111_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.112 已实现 durable worker heartbeat/liveness registry：新增 `platform_worker_heartbeats` 持久表、Platform Harness record/list 方法、active/stale liveness classification、`GET /api/v1/platform/harness/worker-heartbeats`，并把 `PlatformHarnessWorkerRunner` 的 poll、claim、renew、failure、finish、idle 生命周期写入 heartbeat registry。该版本只关闭 worker heartbeat registry slice，不声明 distributed queue、process supervision、external alerting、real worker-offload handlers 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.112/evidence_v0.2.112_e08_distributed_heartbeat_registry_summary.md`

v0.2.113 remaining sidecar slice reselection 已排除 completed heartbeat registry、handler catalog、stdio/container egress、secret rotation、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `scheduler_trigger_worker_offload_handler` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 scheduler trigger worker handler，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.113/decision_v0.2.113_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.114 已实现 `scheduler_trigger` worker offload handler：worker catalog 将 `scheduler_trigger` 从 deterministic unavailable 迁移为 implemented，`WorkflowScheduler` 增加可选 offload mode，offload tick 会 claim schedule fire 并排队 `scheduler_trigger` task，`PlatformHarnessWorkerRunner` 可消费该 task、复用现有 scheduler/runtime 创建真实 workflow run，并保留 `scheduler_fire` usage 与 heartbeat registry 证据。该版本只关闭 scheduler_trigger worker handler slice，不声明 production worker supervision、distributed queue、剩余非 scheduler handlers 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.114/evidence_v0.2.114_e08_scheduler_trigger_worker_offload_handler_summary.md`

v0.2.115 remaining sidecar slice reselection 已排除 completed scheduler_trigger worker offload handler、heartbeat registry、handler catalog、stdio/container egress、secret rotation、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `workflow_run_worker_offload_handler` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 workflow_run worker handler，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.115/decision_v0.2.115_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.116 已实现 `workflow_run` worker offload handler：worker catalog 将 `workflow_run` 从 deterministic unavailable 迁移为 implemented，`PlatformHarnessWorkerRunner` 可消费 queued `workflow_run` task，并通过现有 `WorkflowRuntime.create_run()` 创建真实 workflow run；created run 的 Platform Harness task 以 worker task 为 parent，worker-created run 使用 `origin=worker`，既有 API run 仍保持 `origin=api`。该版本只关闭 workflow_run worker handler slice，不声明 builder/test/benchmark/draft-preview handlers、production worker supervision、distributed queue 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.116/evidence_v0.2.116_e08_workflow_run_worker_offload_handler_summary.md`

v0.2.117 remaining sidecar slice reselection 已排除 completed workflow_run worker offload handler、scheduler_trigger worker offload handler、heartbeat registry、handler catalog、stdio/container egress、secret rotation、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `test_suite_worker_offload_handler` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 test_suite worker handler，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.117/decision_v0.2.117_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.118 已实现 `test_suite` worker offload handler：worker catalog 将 `test_suite` 从 deterministic unavailable 迁移为 implemented，`PlatformHarnessWorkerRunner` 可消费 queued `test_suite` task，并复用现有 `WorkflowRuntime.run_test_suite()` 执行 draft validation、per-test workflow run、assertion/report 逻辑；per-test workflow run 以 worker test-suite task 为 parent，既有 API `/tests/run` path 仍由 runtime 管理。该版本只关闭 test_suite worker handler slice，不声明 builder/benchmark/draft-preview handlers、production worker supervision、distributed queue 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.118/evidence_v0.2.118_e08_test_suite_worker_offload_handler_summary.md`

v0.2.119 remaining sidecar slice reselection 已排除 completed test_suite、workflow_run、scheduler_trigger worker offload handlers 以及 heartbeat registry、handler catalog、stdio/container egress、secret rotation、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `draft_patch_preview_worker_offload_handler` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 draft_patch_preview worker handler，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.119/decision_v0.2.119_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.120 已实现 `draft_patch_preview` worker offload handler：worker catalog 将 `draft_patch_preview` 从 deterministic unavailable 迁移为 implemented，`PlatformHarnessWorkerRunner` 可消费 queued `draft_patch_preview` task，并复用现有 deterministic `DraftPatchPreviewer.preview()` 产生 preview operations；worker path 和既有 API `/draft/preview-patch` path 均保持不修改 draft revision/content_hash。该版本只关闭 draft_patch_preview worker handler slice，不声明 builder_build/benchmark handlers、production worker supervision、distributed queue 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.120/evidence_v0.2.120_e08_draft_patch_preview_worker_offload_handler_summary.md`

v0.2.121 remaining sidecar slice reselection 已排除 completed draft_patch_preview、test_suite、workflow_run、scheduler_trigger worker offload handlers 以及 heartbeat registry、handler catalog、stdio/container egress、secret rotation、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `benchmark_worker_offload_handler` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 benchmark worker handler，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.121/decision_v0.2.121_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.122 已实现 `benchmark` worker offload handler：worker catalog 将 `benchmark` 从 deterministic unavailable 迁移为 implemented，`PlatformHarnessWorkerRunner` 可消费 queued benchmark case/suite tasks，并复用现有 deterministic `BuilderBenchmark.evaluate()` / `evaluate_suite()` 产生 report；suite worker task 继续记录 node_execution usage，既有 API benchmark path 和 history retrieval 保持可用。该版本只关闭 benchmark worker handler slice，不声明 builder_build handler、production worker supervision、distributed queue、external KMS 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.122/evidence_v0.2.122_e08_benchmark_worker_offload_handler_summary.md`

v0.2.123 remaining sidecar slice reselection 已排除 completed benchmark、draft_patch_preview、test_suite、workflow_run、scheduler_trigger worker offload handlers 以及 heartbeat registry、handler catalog、stdio/container egress、secret rotation、policy-controls API、Studio controls 和 operator runbook lifecycle，并选择 `builder_build_worker_offload_handler` 作为下一条 E08 实现切片。该版本只选择下一 slice，不实现 builder_build worker handler，也不声明 full sidecar completion。

证据：`../../workingon-archives/v0.2.123/decision_v0.2.123_e08_remaining_sidecar_slice_reselection_summary.md`

v0.2.124 已实现 `builder_build` worker offload handler：worker catalog 将最后一个 required task kind 从 unavailable 迁移为 implemented，`PlatformHarnessWorkerRunner` 可消费 queued builder_build task，并复用现有 Builder lifecycle 执行 publish/needs_attention 状态转换；API build path 仍由 `Builder.start()` 管理自己的 harness task。该版本关闭 required worker task-kind execution coverage，但不声明 production worker supervision、distributed queue、external KMS 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.124/evidence_v0.2.124_e08_builder_build_worker_offload_handler_summary.md`

v0.2.125 remaining sidecar architecture reselection 已排除 completed `builder_build` 和全部 required worker task-kind execution coverage 证据，并选择 `production_worker_supervision` 作为下一条 E08 architecture slice。该版本只选择下一 slice，不实现 production worker supervision，也不声明 distributed queue、external KMS 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.125/decision_v0.2.125_e08_remaining_sidecar_architecture_reselection_summary.md`

v0.2.126 已实现 in-process production worker supervision：新增 `PlatformWorkerSupervisor`，提供 worker supervision snapshot/start/stop API，可启动、观察、停止一个受监督的 Platform Harness worker loop，并通过 heartbeat 和 recent results 证明实际 worker execution。该版本只关闭 in-process supervision slice，不实现 distributed queue semantics、external process manager、external KMS provider integration 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.126/evidence_v0.2.126_e08_production_worker_supervision_summary.md`

v0.2.127 remaining sidecar architecture reselection 已排除 completed in-process production worker supervision 和全部 required worker task-kind execution coverage 证据，并选择 `distributed_queue_semantics` 作为下一条 E08 architecture slice。该版本只选择下一 slice，不实现 distributed queue semantics，也不声明 external process manager、external KMS 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.127/decision_v0.2.127_e08_remaining_sidecar_architecture_reselection_summary.md`

v0.2.128 已实现 storage-backed distributed queue semantics：新增 atomic claim-next 队列领取、expired lease requeue 语义、queue semantics snapshot 与 requeue API，并将 `PlatformHarnessWorkerRunner` 的消费路径改为 claim-next。该版本只关闭队列所有权/requeue semantics，不实现 external process manager、external KMS provider integration 或 full sidecar completion。

证据：`../../workingon-archives/v0.2.128/evidence_v0.2.128_e08_distributed_queue_semantics_summary.md`
