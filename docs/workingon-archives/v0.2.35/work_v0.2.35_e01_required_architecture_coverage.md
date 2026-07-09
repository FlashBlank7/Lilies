# v0.2.35 E01 Required Architecture Coverage

## Goal

Make the complex required-mode benchmark case require visible architecture coverage in the Builder-facing requirement itself, then rerun required mode with the repaired experiment contract.

The immediate blocker from v0.2.34 is: retry reached `ready`, but the ready graph missed `context_assembler`.

## Source Task Set Disposition

| Prior next-stage task | v0.2.35 disposition | Reason |
| --- | --- | --- |
| Repair required-mode complex case so ready build also satisfies visible architecture coverage | accepted | Direct blocker from v0.2.34. |
| Re-run or re-evaluate required-mode complex case after narrow repair | accepted | Needed to validate contract repair. |
| Run E02 human-review experiment | deferred | Separate experiment stage. |
| Run E04 local repair vs full rebuild comparison | deferred | Separate experiment stage. |
| Run E05 reuse-depth comparison | deferred | Separate experiment stage. |
| Run E08 sidecar/passmode comparison | deferred | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | deferred | Separate Platform Harness stage. |

## Linked Designs

- `docs/current-design/design_v0.2.35_complex_case_architecture_contract_v1.md`
- `docs/current-design/design_v0.2.35_required_architecture_rerun_v1.md`

## Execution Plan

| Step | Status | Evidence |
| --- | --- | --- |
| Strengthen complex case requirement with explicit required node list | completed | `scripts/live_builder_benchmark_suite.py` |
| Add deterministic test for requirement contract | completed | `2 passed, 1 warning` focused pytest |
| Run paid/live required rerun | completed | three v0.2.35 JSON evidence files |
| Compare against v0.2.34 retry | completed | `docs/experiment-status/evidence/experiment_v0.2.35_e01_required_architecture_coverage_summary_2026_07_09.json` |
| Generate DOCX experiment report | completed | `docs/experiment-status/reports/2026-07-09_1944_E01_required_architecture_coverage_after_json_recovery.docx` |
| Update experiment ledger | completed | `docs/experiment-status/v0.2_experiment_status.md` |
| Archive stage designs and workingon by version | pending | historical designs and stage report |

## Acceptance Criteria

- The complex case requirement explicitly names `context_assembler` and the complete required node type list.
- The required rerun uses paid/live configured model keys, `max_repair_cycles=3`, and bounded timeout.
- The result is honestly classified:
  - `closed` only if build is `ready` and benchmark passes.
  - `partial` if only one side is satisfied.
- Active `docs/current-design/` and `docs/workingon/` are empty except README after archive.

## Current Status

Design 1 completed.

Design 2 initial paid/live reruns exposed a narrower runtime failure:

- First v0.2.35 rerun: visible architecture coverage passed, but build ended `needs_attention` because `draft_add_node` tool input JSON was invalid.
- Retry rerun: visible architecture coverage passed, but build ended `needs_attention` because `draft_update_node` tool input JSON was invalid.
- Decision: continue current version with a new accepted design slice, `design_v0.2.35_invalid_tool_json_recovery_v1.md`, because the stage target is not closed until the same complex required case can reach `ready` or the remaining blocker is explicitly proven.

Focused verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_live_builder_benchmark_case_registry_supports_complex_case tests/test_workflow.py::test_live_builder_benchmark_reads_max_repair_cycles_env -q
```

Result: `2 passed, 1 warning`.

Compile:

```bash
.venv/bin/python -m compileall -q scripts/live_builder_benchmark_suite.py tests/test_workflow.py
```

Result: passed.

## Current Version Continuation

| Continuation task | Status | Evidence |
| --- | --- | --- |
| Convert invalid tool input JSON into a recoverable tool_result instead of aborting the Builder loop | completed | `platform/backend/src/agent_platform/runtime.py`, `platform/backend/src/agent_platform/builder.py` |
| Add deterministic regression for invalid tool JSON recovery | completed | `tests/test_runtime.py::test_runtime_feeds_invalid_tool_json_back_to_model` |
| Run focused tests after runtime recovery change | completed | `3 passed, 1 warning` |
| Run paid/live complex required rerun after runtime recovery change | completed | `experiment_v0.2.35_e01_required_architecture_coverage_after_json_recovery_2026_07_09.json`: `status=passed`, `build_status=ready`, benchmark passed |

## Final Result

v0.2.35 completed the narrow target from v0.2.34:

- v0.2.34 ready graph missed `context_assembler`.
- v0.2.35 explicit architecture contract made the Builder-facing requirement preserve `context_assembler`; first and retry runs benchmarked successfully but failed build readiness due malformed tool input JSON.
- v0.2.35 runtime JSON recovery converted that failure mode into recoverable tool_result semantics.
- Fresh paid/live rerun after the recovery change reached `ready` and benchmark passed.

Closure boundary:

- Completed: complex required case can now satisfy ready + visible architecture coverage.
- Not closed: original E01 as a broad product strategy question. More complex cases or a fuller A/B suite are still required before defaulting all complex builds to required plan-first.
