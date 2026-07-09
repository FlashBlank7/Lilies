# work_v0.2.44_customer_support_template_reuse_repair

## 1. Goal

Repair the concrete customer-support Template reuse gaps exposed by v0.2.43, without pretending to close global E05.

This stage focuses on deterministic engineering fixes before any new paid/live rerun:

- make E05 benchmark/result reporting separate Builder terminal status, suite pass, and case pass;
- make marketplace `template_expand` return validation and template contract information;
- add Builder mutation guardrails so post-template customization does not silently drift into unreachable graphs or mandatory-test required-node mismatches.

## 2. Scope

Included:

- E05 runner result schema/reporting compatibility improvements.
- Builder template_expand result contract improvements.
- Builder mutation validation feedback and destructive guard for required node types.
- Deterministic tests for customer-support template expansion, benchmark outcome summarization, and required-node drift prevention.

Excluded:

- New paid/live rerun before deterministic repair evidence lands.
- UI changes for `max_elapsed_seconds`.
- Full E05 closure.
- E08 sidecar/passmode work.

## 3. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.43_e05_multifamily_with_build_watchdog.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Repair customer-support Template reuse reliability | accepted | `design_v0.2.44_template_expand_contract_and_validation_v1.md`; `design_v0.2.44_builder_mutation_guardrails_v1.md` | Directly follows v0.2.43 invalid draft and required-node drift evidence. |
| Improve E05 result reporting | accepted | `design_v0.2.44_e05_result_reporting_semantics_v1.md` | Needed because v0.2.43 showed suite pass can hide case failure. |
| Decide whether to rerun customer-support E05 after repair | deferred | none | Must wait for deterministic repair evidence. |
| Add UI/API visibility improvements for Builder `max_elapsed_seconds` | deferred | none | Separate product/UI stage. |
| Consider post-agent-loop watchdog coverage | deferred | none | No clean post-agent-loop hang evidence yet. |
| Run E08 workflow-internal gate vs sidecar monitor/passmode comparison | deferred | none | Separate Harness experiment stage. |
| Continue deferred Platform Harness product tasks | deferred | none | Separate product/platform stage. |
| Run actual E02 human-panel review if available | deferred | none | No reviewer pool in this context. |
| Broaden E04 failure classes | deferred | none | Separate repair-policy experiment. |
| Add more complex plan-first cases | deferred | none | Optional product-strategy evidence. |

Every next-stage task is listed and dispositioned.

## 4. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Improve E05 reporting semantics | `docs/current-design/design_v0.2.44_e05_result_reporting_semantics_v1.md` | completed | Runner exposes suite pass, case pass, case score, and missing coverage separately; tests cover legacy-compatible summary. |
| Expose template expansion contract and validation | `docs/current-design/design_v0.2.44_template_expand_contract_and_validation_v1.md` | completed | `template_expand` result includes validation, node types, and marketplace template contract; customer-support expansion test covers it. |
| Add Builder mutation guardrails | `docs/current-design/design_v0.2.44_builder_mutation_guardrails_v1.md` | completed | Destructive mutations surface validation feedback and prevent mandatory test required-node drift; deterministic test covers removal guard. |

## 5. Evidence

Code changes:

- `scripts/e05_template_reuse_depth_experiment.py`
- `platform/backend/src/agent_platform/builder.py`
- `tests/test_e05_template_reuse_depth_experiment.py`
- `tests/test_workflow.py`

Focused verification:

- `.venv/bin/python -m pytest tests/test_e05_template_reuse_depth_experiment.py -q` -> `7 passed, 1 warning`
- `.venv/bin/python -m pytest tests/test_workflow.py::test_builder_customer_support_template_expand_returns_contract_and_validation tests/test_workflow.py::test_builder_refuses_to_remove_last_node_required_by_mandatory_test tests/test_e05_template_reuse_depth_experiment.py -q` -> `9 passed, 1 warning`

Regression:

- `.venv/bin/python -m pytest -q` -> `108 passed, 1 warning`
- `.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts` -> passed

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_v0.2.44_e05_result_reporting_semantics_v1.md` | completed | E05 now exposes `benchmark_outcome` and defaults future suite pass rate to `1.0`. | Archive after stage report. |
| `design_v0.2.44_template_expand_contract_and_validation_v1.md` | completed | `template_expand` returns validation, node types, and marketplace contract. | Archive after stage report. |
| `design_v0.2.44_builder_mutation_guardrails_v1.md` | completed | Builder blocks destructive required-node drift and returns validation feedback. | Archive after stage report. |

## 7. Archive Conditions

- All three accepted designs are implemented or explicitly blocked with evidence.
- Focused tests for changed paths pass.
- Full regression and compileall pass.
- Experiment ledger is updated if E05 status changes.
- Stage report is created.
- Historical designs use `v0.2.44_` filenames.
- Active `current-design/` and `workingon/` are cleared after archive.
