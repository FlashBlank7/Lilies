# work_v0.2.45_customer_support_e05_repair_rerun

## 1. Goal

Run a bounded paid/live E05 customer-support rerun after v0.2.44 deterministic repair.

The goal is to determine whether v0.2.44 improved the interpretability and reliability of customer-support Template reuse behavior. This stage still must not close global E05 unless the evidence is genuinely broad enough.

## 2. Scope

Included:

- Run E05 none/shallow/deep on `customer_support_router`.
- Use `E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS=600`.
- Use new `benchmark_outcome` fields in analysis.
- Compare with v0.2.43.
- Produce raw JSON, chart, DOCX report, and ledger update.

Excluded:

- Changing Builder behavior during the rerun unless deterministic failure blocks the experiment.
- Global E05 closure.
- UI changes.

## 3. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.44_customer_support_template_reuse_repair.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Run bounded paid/live customer-support E05 rerun after repair | accepted | `design_v0.2.45_customer_support_paid_rerun_report_v1.md` | Recommended automatic handoff and required to validate v0.2.44 repair. |
| Decide whether v0.2.44 repair changed customer-support reuse behavior | accepted | `design_v0.2.45_customer_support_paid_rerun_report_v1.md` | Analysis must compare with v0.2.43. |
| Add UI/API visibility improvements for Builder `max_elapsed_seconds` | deferred | none | Separate product/UI stage. |
| Consider post-agent-loop watchdog coverage | deferred | none | Await evidence. |
| Run E08 workflow-internal gate vs sidecar monitor/passmode comparison | deferred | none | Separate Harness experiment. |
| Continue deferred Platform Harness product tasks | deferred | none | Separate product/platform stage. |
| Run actual E02 human-panel review if available | deferred | none | No reviewer pool. |
| Broaden E04 failure classes | deferred | none | Separate repair-policy experiment. |
| Add more complex plan-first cases | deferred | none | Optional product-strategy evidence. |

Every next-stage task is listed and dispositioned.

## 4. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Run paid/live customer-support rerun and report | `docs/current-design/design_v0.2.45_customer_support_paid_rerun_report_v1.md` | completed | JSON, chart, DOCX, comparison, ledger update. |

## 5. Evidence

Paid/live result:

- Raw JSON: `docs/experiment-status/evidence/experiment_v0.2.45_e05_customer_support_rerun_2026_07_10.json`
- Chart: `docs/experiment-status/evidence/experiment_v0.2.45_e05_customer_support_rerun_summary.png`
- DOCX: `docs/experiment-status/reports/2026-07-10_0103_E05_customer_support_rerun_after_guardrails.docx`
- Implementation evidence: `docs/workingon/implementation_v0.2.45_customer_support_e05_repair_rerun.md`

Arm results:

| Depth | Build status | Elapsed | Template evidence | Benchmark outcome | Interpretation |
| --- | --- | --- | --- | --- | --- |
| `none` | `published` | `198.961s` | `template_suggestions=1`, `template_expands=0` | case passed, score `0.85` | no-reuse path is stable for this case after v0.2.44 reporting semantics. |
| `shallow` | `ready` | `545.849s` | `template_suggestions=1`, `template_expands=1` | case passed, score `0.85` | v0.2.44 repair is positively validated for shallow customer-support reuse. |
| `deep` | `needs_attention` | `602.071s` | `template_suggestions=1`, `template_expands=1` | case passed, score `0.85` | structure is benchmark-clean, but build hit `BuildDeadlineExceeded` at `600.004s`. |

Comparison to v0.2.43:

- `shallow` improved from provider stream timeout / `needs_attention` to `ready`.
- `deep` improved from invalid draft to benchmark-clean draft, but remains `needs_attention` because of build-level deadline.
- `none` improved from published with benchmark missing `if_else` to published with benchmark case pass.

Ledger decision:

- Mark as `验证应用 / 原始 E05 未关闭`.
- Do not close global E05 because evidence is still one customer-support task family and deep reuse remains long-chain unstable.

## 6. Archive Conditions

- [x] Paid/live run completes or a real blocker is documented.
- [x] DOCX report exists if run completes.
- [x] Focused and full regression pass after any code/report generation changes.
- [x] Experiment ledger updated without falsely closing E05.
- [x] Stage report created.
- [x] Design and workingon archived by version.
