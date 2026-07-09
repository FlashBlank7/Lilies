# E08 Harness Sidecar Passmode Ledger

状态：工程证据审查完成；sidecar/passmode 对照未关闭

## 当前结论

v0.2.3-v0.2.28 已推进多项 Platform Harness slice：task monitor、observability UI、durable storage、owner budget、stale reconciliation、secret policy、HTTP/stdio egress、worker lease/runner/CLI/renewal 等。它们是重要工程前置，但不等于 E08 sidecar/passmode 对照实验完成。

## 证据

| 项目 | 路径 |
| --- | --- |
| 工程证据审查报告 | `../reports/2026-07-09_1809_E08_harness_sidecar_passmode_evidence_review.docx` |
| 相关 stage chain | `../../stage-reports/v0.2.3_platform_harness_and_development_roadmap.md` 到 `../../stage-reports/v0.2.28_worker_heartbeat_and_renewal.md` |
| 最新 timeout/build-deadline 应用 | `../../stage-reports/v0.2.40_builder_provider_timeout_boundary.md`、`../../stage-reports/v0.2.42_builder_build_level_watchdog.md` |

## 边界

当前 Platform Harness 仍有未闭环方向：allowlist-grade stdio/container egress、KMS/rotation、完整 handler catalog、分布式 heartbeat registry、policy controls 完整 UI/API、长时间运行 runbook。

## 下一步

设计 workflow-internal soft harness block vs platform sidecar/passmode monitor 对照，比较 enforcement 强度、观测粒度、失败隔离、恢复语义和成本。
