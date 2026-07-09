# work_v0.2.38_e05_template_reuse_depth_live_comparison

## 1. Goal

Run E05 as a bounded paid/live experiment comparing `reuse_depth=none`, `reuse_depth=shallow`, and `reuse_depth=deep` for the same Template-friendly BlockFlow requirement.

The goal is not to claim the Template system is complete. The goal is to determine whether the current v1 template suggestion and reuse mechanism creates measurable generation differences, and to record enough evidence to decide the next engineering slice.

## 2. Scope

Included:

- One controlled requirement that should match built-in templates.
- Three live Builder Team arms: `none`, `shallow`, `deep`.
- Deterministic metrics: build status, benchmark score, draft node/edge/test counts, BuildPlan reuse depth, template suggestion calls, template expand calls, and recommended actions observed in stream evidence.
- Bounded paid model execution using configured environment keys.
- Concise DOCX report with background, experiment design, result/conclusion, screenshot/figure placeholder, evidence chain, and engineering application status.
- Experiment ledger update.

Excluded:

- Embedding RAG implementation.
- Template marketplace UI.
- Human qualitative review of generated workflows.
- General conclusion that Template reuse is always good or bad.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Define controlled E05 runner and metrics | `docs/current-design/design_v0.2.38_e05_reuse_depth_case_and_runner_v1.md` | completed | Runner exposes three arms and extracts stream/template metrics deterministically. |
| Run paid/live comparison and publish report | `docs/current-design/design_v0.2.38_e05_paid_depth_comparison_and_report_v1.md` | completed | Evidence JSON, DOCX report, ledger update, stage report, and archive are complete. |

## 4. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.37_e04_local_repair_vs_full_rebuild.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Run E05 none/shallow/deep template reuse-depth live generation comparison. | accepted | `design_v0.2.38_e05_reuse_depth_case_and_runner_v1.md`; `design_v0.2.38_e05_paid_depth_comparison_and_report_v1.md` | Recommended handoff and original E05 backlog require this controlled comparison. |
| Run E08 workflow-internal gate vs sidecar monitor/passmode comparison. | deferred | none | Separate Harness experiment; keep after E05 unless E05 exposes a blocker. |
| Continue deferred Platform Harness product tasks with explicit closure level. | deferred | none | Separate product/platform boundary stage; not part of E05 research experiment. |
| Optionally run actual E02 human-panel review if a human reviewer pool is available. | deferred | none | Human reviewer pool is not available in this execution context. |
| Optionally add broader E04 failure types before making a general repair-vs-rebuild policy. | deferred | none | E04 broadening is valuable but follows E05 in the current handoff. |
| Optionally add more complex plan-first cases before making a product-default E01 decision. | deferred | none | E01 complex case has a current ready+coverage closure slice; not selected for this version. |

Every next-stage task is listed. Only E05 is accepted for v0.2.38; the others are explicitly deferred.

## 5. Evidence

- `scripts/e05_template_reuse_depth_experiment.py`
- `docs/experiment-status/evidence/experiment_v0.2.38_e05_template_reuse_depth_2026_07_09.json`
- `docs/experiment-status/evidence/experiment_v0.2.38_e05_template_reuse_depth_calls.png`
- `docs/experiment-status/reports/2026-07-09_2051_E05_template_reuse_depth_live_comparison.docx`
- Focused pytest: `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` -> `3 passed, 1 warning`.
- DOCX QA: zip structure passed; readback found `背景`、`实验设计`、`结果结论`、`图片或截图`、`证据链`、`工程应用状态`; visual render QA failed because `soffice` is unavailable.
- Paid/live result: `none` published with benchmark score `0.85`, `shallow` and `deep` reached `needs_attention` after template suggestion/expand activity.
- Design correction evidence: an initial run was interrupted after revealing that E05 runner should record runtime model and shorter provider timeout; final run used `E05_REUSE_DEPTH_RUN_ID=v0_2_38_rerun` and `E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=120`.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_v0.2.38_e05_reuse_depth_case_and_runner_v1.md` | proceed to next design | Script and focused tests completed; evidence JSON includes template operation metrics. | Completed. |
| `design_v0.2.38_e05_paid_depth_comparison_and_report_v1.md` | completed | Paid/live run, DOCX report, ledger update, and QA completed. | Archive v0.2.38. |

## 7. Review Before Archive

- Completion summary: E05 three-arm paid/live comparison completed; result does not support deeper reuse as currently implemented.
- Files changed: script, focused tests, evidence JSON/PNG, DOCX report, experiment ledger, work/design docs.
- Verification: focused pytest passed; full pytest pending before archive.
- Remaining risk: visual DOCX QA unavailable; no engineering fix has yet applied the E05 result.
- All next-stage tasks dispositioned: yes
- All accepted tasks expanded into designs: yes
- Every accepted design completed or explicitly blocked/deferred: yes
- Engineering closure level claimed: research experiment
- Engineering closure actually achieved: research experiment
- Partial slices carried forward: marketplace Template expandability contract repair and E05 rerun
- Active current-design will be cleared after archive: yes
- Active workingon will be cleared after archive: yes
- Minor version target closure: completed
- Experiment deliverables, if any: DOCX report plus JSON evidence produced
- Awaiting user review before archive: no, Automatic Evolution Mode archives automatically

## 8. Archive Conditions

- Both design files completed or explicitly blocked with evidence.
- Three arms either executed or skipped with concrete credential/service blocker.
- Experiment ledger updated.
- DOCX structural QA completed; visual QA attempted if LibreOffice is available.
- Historical designs written with `v0.2.38_` filenames.
- Active `docs/current-design/` and `docs/workingon/` cleared to README only.
- Commit created with explicit staged path list.

## 9. Automatic Evolution

- Automatic Evolution Mode active: yes
- Current version: `v0.2.38`
- Archive automatically after verification: yes
- Next version selection source: current stage report to be created after completion
- Continue after archive: yes
