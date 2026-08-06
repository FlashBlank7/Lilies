# work_v0.2.29_formal_experiment_tranche

## 1. Goal

Run the first formal experiment tranche for the unresolved v0.2 experiment backlog and convert the evidence into readable DOCX reports and an updated experiment ledger.

This stage is a research experiment slice. It does not claim to close E01/E02/E04/E05/E08 unless each original experiment question has enough evidence. Partial or failed results must remain visible.

## 2. Scope

Included:

- Use the real paid/live Builder benchmark evidence generated for v0.2.29.
- Produce concise DOCX experiment reports for E01, E02, E04, E05, and E08.
- Update `docs/experiment-status/v0.2_experiment_status.md`.
- Archive the stage with historical design records and workingon evidence.

Excluded:

- New backend feature work unless an experiment exposes a narrow deterministic failure that must be fixed before reporting.
- Browser visual QA.
- Editable policy controls.
- Allowlist-grade stdio MCP firewalling.
- KMS/key rotation/legacy secret migration.

## 3. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.28_worker_heartbeat_and_renewal.md`

| Prior next-stage task | Disposition | Current design | Reason |
| --- | --- | --- | --- |
| Add more real worker handlers for build/test/workflow-run tasks | Deferred | none | This is a Platform Harness worker catalog stage, not the formal experiment tranche. |
| Run a formal paid/live experiment tranche for E01/E02/E04/E05/E08 and produce DOCX reports | Accepted | `docs/current-design/design_v0.2.29_paid_builder_evidence_v1.md`; `docs/current-design/design_v0.2.29_experiment_docx_reports_v1.md`; `docs/current-design/design_v0.2.29_experiment_status_ledger_v1.md` | This is the recommended v0.2.29 handoff. |
| Add browser visual QA for Platform Harness and Builder product surfaces | Deferred | none | Separate UI QA stage. |
| Add editable Platform Harness policy controls if product requirements need runtime configuration | Deferred | none | Product decision and API mutation design required. |
| Design allowlist-grade stdio MCP sandbox firewalling if stdio allowlist remains a product requirement | Deferred | none | Separate sandbox/firewall design required. |
| Add KMS/external secret manager, key rotation, or legacy secret migration | Deferred | none | Later secret hardening stage. |

## 4. Execution Evidence

Paid/live Builder command:

```bash
LIVE_BUILDER_BENCHMARK_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.29_paid_builder_tranche_2026_07_09.json \
LIVE_BUILDER_BENCHMARK_MAX_TURNS=24 \
LIVE_BUILDER_BENCHMARK_TIMEOUT_SECONDS=900 \
.venv/bin/python scripts/live_builder_benchmark_suite.py
```

Observed result:

- Evidence file: `docs/experiment-status/evidence/experiment_v0.2.29_paid_builder_tranche_2026_07_09.json`
- Runner mode: `inprocess`
- Provider/model evidence: DeepSeek configured; Platform Harness usage records show `deepseek-v4-pro`.
- Build status: `needs_attention`
- Script status: `build_failed_benchmark_evaluated`
- Model calls: `24`
- Tool calls: `47`
- Benchmark score: `0.733`
- Pass rate: `0.0`
- Missing required node type: `end`

Focused deterministic verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_run_suite_returns_readable_test_frame_report tests/test_workflow.py::test_template_suggestions_include_reuse_depth_actions tests/test_workflow.py::test_platform_harness_tracks_test_suite_and_workflow_usage tests/test_workflow.py::test_platform_harness_worker_runner_renews_lease_for_long_handler -q
```

Result: `4 passed, 1 warning`.

Generated reports:

- `docs/experiment-status/reports/2026-07-09_1809_E01_plan_first_paid_builder_baseline.docx`
- `docs/experiment-status/reports/2026-07-09_1809_E02_readable_testframe_artifact_review.docx`
- `docs/experiment-status/reports/2026-07-09_1809_E04_local_repair_vs_rebuild_evidence_review.docx`
- `docs/experiment-status/reports/2026-07-09_1809_E05_template_reuse_depth_artifact_review.docx`
- `docs/experiment-status/reports/2026-07-09_1809_E08_harness_sidecar_passmode_evidence_review.docx`

Generated summary:

- `docs/experiment-status/evidence/experiment_v0.2.29_formal_tranche_summary_2026_07_09.json`

DOCX QA:

- Zip structure check: passed.
- Paragraph/readback check: every report contains `背景`, `实验设计`, `结果结论`, `图片或截图`, and `证据链`.
- Visual render QA: skipped because `soffice` / `libreoffice` is not available on this machine.

## 5. Design Execution Status

| Design | Status | Evidence | Proceed |
| --- | --- | --- | --- |
| `design_v0.2.29_paid_builder_evidence_v1.md` | Completed | Raw JSON and summary JSON recorded; every scoped experiment classified honestly | yes |
| `design_v0.2.29_experiment_docx_reports_v1.md` | Completed | Five DOCX reports generated; structural QA passed; render QA unavailable | yes |
| `design_v0.2.29_experiment_status_ledger_v1.md` | Completed | `docs/experiment-status/v0.2_experiment_status.md` updated to v0.2.29; backlog statuses remain honest | yes |

## 6. Completion Gate

- All source tasks dispositioned: yes
- All accepted tasks expanded into design: yes
- All accepted designs completed or explicitly blocked/deferred: yes
- DOCX reports created: yes
- Experiment ledger updated: yes
- Render QA: structural QA passed; visual render QA unavailable because LibreOffice is missing
- This file records current stage evidence only and does not guide the next stage: yes
