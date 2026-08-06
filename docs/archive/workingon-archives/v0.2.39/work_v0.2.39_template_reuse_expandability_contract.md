# work_v0.2.39_template_reuse_expandability_contract

## 1. Goal

Apply the E05 result by fixing the Template reuse expandability contract: Builder-facing template suggestions must point to workflows that Builder can actually expand into the draft, or the suggestion path must declare that expansion is unavailable.

This version also reruns or re-evaluates E05 after the fix so the experiment ledger can record whether the engineering result is `已应用` or still only a hypothesis.

## 2. Scope

Included:

- Builder `template_list` should expose both server-defined templates and marketplace `TemplateStore` templates.
- Builder `template_expand` should expand marketplace templates from `TemplateStore` when the suggested template name belongs to the marketplace.
- Builder `template_expand` should keep existing server-defined architecture template support.
- Deterministic tests should prove `code_reviewer` can be suggested and expanded by Builder into an editable draft.
- E05 evidence should be re-evaluated after the fix with bounded paid/live acceptance if credentials are available.
- Experiment ledger should mark the E05 engineering change as `已应用` only if validation evidence supports it.

Excluded:

- Embedding RAG.
- Template marketplace UI.
- Template merge/rating changes.
- Fixing invalid built-in `drone_intervention_v1.json`.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Implement marketplace template expandability contract | `docs/current-design/design_v0.2.39_marketplace_template_expand_contract_v1.md` | completed | Builder can list and expand `TemplateStore` workflows such as `code_reviewer` without breaking `claude_like_coding_agent`. |
| Rerun E05 after the expandability fix | `docs/current-design/design_v0.2.39_e05_after_expandability_fix_validation_v1.md` | completed | Evidence shows whether the engineering fix improves shallow/deep reuse behavior, with DOCX/report and ledger update. |

## 4. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.38_e05_template_reuse_depth_live_comparison.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Fix the Template reuse expandability contract exposed by E05. | accepted | `design_v0.2.39_marketplace_template_expand_contract_v1.md` | This is the smallest deterministic engineering fix implied by E05. |
| Rerun E05 after the fix to decide applied status. | accepted | `design_v0.2.39_e05_after_expandability_fix_validation_v1.md` | Required before marking E05 result `已应用` or `验证应用`. |
| Run E08 workflow-internal gate vs sidecar monitor/passmode comparison. | deferred | none | Separate Harness experiment after Template reuse fix. |
| Continue deferred Platform Harness product tasks with explicit closure level. | deferred | none | Separate product/platform boundary stage. |
| Run actual E02 human-panel review if a human reviewer pool becomes available. | deferred | none | No human reviewer pool in this execution context. |
| Broaden E04 failure classes. | deferred | none | Separate repair-policy experiment. |
| Add more complex plan-first cases. | deferred | none | Optional product-strategy evidence after current E05 application. |

Every next-stage task is listed and dispositioned.

## 5. Evidence

- Implementation evidence: `docs/workingon/implementation_v0.2.39_template_reuse_expandability_contract.md`
- Deterministic tests:

```bash
.venv/bin/python -m pytest \
  tests/test_e05_template_reuse_depth_experiment.py \
  tests/test_workflow.py::test_builder_can_expand_claude_like_template_into_editable_draft \
  tests/test_workflow.py::test_builder_template_list_includes_marketplace_and_server_defined_templates \
  tests/test_workflow.py::test_builder_can_expand_marketplace_template_into_editable_draft \
  -q
```

Result: `6 passed, 1 warning`.

- Paid/live validation:

```bash
E05_REUSE_DEPTH_EXPERIMENT_VERSION=v0.2.39 \
E05_REUSE_DEPTH_RUN_ID=v0.2.39_e05_after_expandability_fix_20260709 \
E05_REUSE_DEPTH_RESULT_PATH=docs/experiment-status/evidence/experiment_v0.2.39_e05_after_expandability_fix_2026_07_09.json \
E05_REUSE_DEPTH_MAX_TURNS=42 \
E05_REUSE_DEPTH_MAX_REPAIR_CYCLES=2 \
E05_REUSE_DEPTH_TIMEOUT_SECONDS=900 \
E05_REUSE_DEPTH_PROVIDER_TIMEOUT_SECONDS=180 \
.venv/bin/python scripts/e05_template_reuse_depth_experiment.py
```

Result: `status=completed`; `deep` arm `published`; `shallow/deep` both expanded `code_reviewer` with `source=marketplace`.

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.39_e05_after_expandability_fix_2026_07_09.json`
- DOCX report: `docs/experiment-status/reports/2026-07-09_2305_E05_after_expandability_fix_validation.docx`

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_v0.2.39_marketplace_template_expand_contract_v1.md` | proceed to next design | Code, deterministic tests, and paid/live evidence prove marketplace expansion contract. | Archive design after stage report. |
| `design_v0.2.39_e05_after_expandability_fix_validation_v1.md` | completed | Paid/live validation completed with DOCX report and ledger update. | Archive design after stage report. |

## 7. Review Before Archive

- Completion summary: marketplace Template expandability contract fixed and validated; E05 original quality-benefit question remains open.
- Files changed: `builder.py`, E05 script, workflow tests, experiment status ledger, E05 evidence/report.
- Verification: focused deterministic tests passed; paid/live E05 post-fix validation completed.
- Remaining risk: deep reuse success needs more cases; `none`/`shallow` still ended `needs_attention`; provider timeout behavior remains a follow-up.
- All next-stage tasks dispositioned: yes
- All accepted tasks expanded into designs: yes
- Every accepted design completed or explicitly blocked/deferred: yes
- Engineering closure level claimed: backend slice + research validation
- Engineering closure actually achieved: backend slice + research validation
- Partial slices carried forward: E05 quality-benefit closure; provider timeout/build timeout boundary.
- Active current-design will be cleared after archive: yes
- Active workingon will be cleared after archive: yes
- Minor version target closure: complete with bounded conclusion
- Experiment deliverables, if any: post-fix E05 evidence/report completed
- Awaiting user review before archive: no, Automatic Evolution Mode archives automatically

## 8. Archive Conditions

- Deterministic tests pass.
- Paid/live validation is executed or blocked with concrete reason.
- Experiment ledger records applied/verified/deferred status honestly.
- Historical designs are written with `v0.2.39_` filenames.
- Active `docs/current-design/` and `docs/workingon/` are cleared to README only.
- Commit created with explicit staged path list.

## 9. Automatic Evolution

- Automatic Evolution Mode active: yes
- Current version: `v0.2.39`
- Archive automatically after verification: yes
- Next version selection source: current stage report to be created after completion
- Continue after archive: yes
