# work_v0.2.43_e05_multifamily_with_build_watchdog

## 1. Goal

Continue E05 with a second Template-friendly task family after v0.2.42 added Builder build-level watchdog support.

The target is to make the E05 runner case-aware, pass `max_elapsed_seconds` to Builder builds, run a non-code-review Template-friendly paid/live validation, and produce a DOCX report. Original E05 must remain open unless the evidence is genuinely broad enough.

## 2. Scope

Included:

- Add E05 runner support for non-code-review cases.
- Add `E05_REUSE_DEPTH_MAX_ELAPSED_SECONDS` so paid/live builds use v0.2.42 watchdog.
- Use `customer_support_router` as the second Template-friendly case.
- Run bounded paid/live none/shallow/deep comparison.
- Produce raw JSON, chart, DOCX report, and ledger update.

Excluded:

- General benchmark framework rewrite.
- UI for selecting E05 cases.
- Closing original E05 globally.
- E08 sidecar/passmode experiment.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Add case-aware E05 runner and watchdog parameter | `docs/current-design/design_v0.2.43_e05_case_runner_watchdog_v1.md` | completed | E05 runner supports `customer_support_router` and passes `max_elapsed_seconds` to Builder. |
| Run customer-support paid/live E05 and report | `docs/current-design/design_v0.2.43_e05_customer_support_paid_report_v1.md` | completed | Paid/live run completed; JSON, chart, and DOCX report generated. |

## 4. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.42_builder_build_level_watchdog.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Continue E05 with additional task families after build-level boundary exists. | accepted | `design_v0.2.43_e05_case_runner_watchdog_v1.md`; `design_v0.2.43_e05_customer_support_paid_report_v1.md` | Recommended handoff and needed to avoid single-case E05 overclaiming. |
| Add UI/API visibility improvements for Builder `max_elapsed_seconds`. | deferred | none | Product/UI slice after backend experiment runner uses the field. |
| Consider extending watchdog coverage to post-agent-loop validation. | deferred | none | Requires evidence that post-agent-loop validation can hang. |
| Run E08 workflow-internal gate vs sidecar monitor/passmode comparison. | deferred | none | Separate Harness experiment stage. |
| Continue deferred Platform Harness product tasks. | deferred | none | Separate product/platform stage. |
| Run actual E02 human-panel review if available. | deferred | none | No human reviewer pool in this context. |
| Broaden E04 failure classes. | deferred | none | Separate repair-policy experiment. |
| Add more complex plan-first cases. | deferred | none | Optional product-strategy evidence. |

Every next-stage task is listed and dispositioned.

## 5. Evidence

Implementation evidence:

- `docs/workingon/implementation_v0.2.43_e05_multifamily_with_build_watchdog.md`

Code and deterministic tests:

- `scripts/e05_template_reuse_depth_experiment.py`
- `tests/test_e05_template_reuse_depth_experiment.py`
- `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` -> `6 passed, 1 warning`
- `.venv/bin/python -m pytest -q` -> `105 passed, 1 warning`
- `.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts` -> passed

Paid/live evidence:

- `docs/experiment-status/evidence/experiment_v0.2.43_e05_customer_support_2026_07_09.json`
- `docs/experiment-status/evidence/experiment_v0.2.43_e05_customer_support_summary.png`
- `docs/experiment-status/reports/2026-07-10_0024_E05_customer_support_reuse_depth.docx`

DOCX QA:

- `unzip -t` passed.
- Structural readback: `25` paragraphs, `3` tables, `1` inline image.
- Visual render QA blocked by missing `soffice`.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_v0.2.43_e05_case_runner_watchdog_v1.md` | completed | Script supports case selection and max_elapsed_seconds; focused tests pass. | Archive after stage report. |
| `design_v0.2.43_e05_customer_support_paid_report_v1.md` | completed | Paid/live run completed and report generated. | Archive after stage report. |

## 7. Review Before Archive

- Completion summary: E05 runner is case-aware and customer-support reuse-depth paid/live evidence is complete.
- Files changed: script/test updates plus experiment evidence/report docs.
- Verification: focused E05 tests passed; full regression and compileall passed.
- Paid/live evidence: completed with DeepSeek `deepseek-v4-pro`.
- DOCX report: generated; structural QA passed; visual QA blocked by missing `soffice`.
- Remaining risk: original E05 remains open; customer_support_router exposes template customization/reachability and benchmark-reporting gaps.
- All next-stage tasks dispositioned: yes
- All accepted tasks expanded into designs: yes
- Every accepted design completed or explicitly blocked/deferred: yes
- Engineering closure level claimed: research experiment + runner capability slice
- Engineering closure actually achieved: research experiment + runner capability slice
- Active current-design will be cleared after archive: yes
- Active workingon will be cleared after archive: yes
- Minor version target closure: accepted v0.2.43 target closed; global E05 remains open by design.
- Awaiting user review before archive: no, Automatic Evolution Mode archives automatically

## 8. Archive Conditions

- E05 runner tests pass.
- Paid/live customer-support run completes or is blocked with a real external reason.
- DOCX report exists if run completes.
- Experiment ledger updated without falsely closing original E05.
- Historical designs are written with `v0.2.43_` filenames.
- Active `docs/current-design/` and `docs/workingon/` are cleared to README only.
- Commit created with explicit staged path list.
