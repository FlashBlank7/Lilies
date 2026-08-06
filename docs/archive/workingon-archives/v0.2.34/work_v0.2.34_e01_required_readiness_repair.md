# v0.2.34 E01 Required-mode Readiness Repair

## Goal

Address the v0.2.33 required-mode complex BlockFlow gap: benchmark structure passed, but Builder stayed `needs_attention` because mandatory tests did not pass.

This version should verify whether the gap is caused by the experiment harness using an overly small repair budget (`max_repair_cycles=1`) before changing Builder capability.

## Source Task Set Disposition

| Prior next-stage task | v0.2.34 disposition | Reason |
| --- | --- | --- |
| Investigate and repair required-mode complex readiness gap | accepted | Direct blocker from v0.2.33. |
| Re-run or re-evaluate complex required-mode case after narrow repair | accepted | Needed to verify the repair. |
| Run E02 human-review experiment | deferred | Separate experiment stage. |
| Run E04 local repair vs full rebuild comparison | deferred | Separate experiment stage. |
| Run E05 reuse-depth comparison | deferred | Separate experiment stage. |
| Run E08 sidecar/passmode comparison | deferred | Separate experiment stage. |
| Continue deferred Platform Harness product tasks | deferred | Separate Platform Harness stage. |

## Linked Designs

- `docs/current-design/design_v0.2.34_benchmark_repair_budget_v1.md`
- `docs/current-design/design_v0.2.34_required_mode_rerun_v1.md`

## Execution Plan

| Step | Status | Evidence |
| --- | --- | --- |
| Add explicit `LIVE_BUILDER_BENCHMARK_MAX_REPAIR_CYCLES` support | completed | `scripts/live_builder_benchmark_suite.py` |
| Record max repair cycles in result JSON | completed | `tests/test_workflow.py::test_live_builder_benchmark_reads_max_repair_cycles_env` |
| Re-run complex required mode with larger bounded repair budget | completed | first run network error; retry reached `ready` |
| Compare v0.2.33 vs v0.2.34 required result | completed | `docs/experiment-status/evidence/experiment_v0.2.34_e01_required_repair_budget_summary_2026_07_09.json` |
| Update experiment ledger and report whether engineering was applied | completed | `docs/experiment-status/v0.2_experiment_status.md` |
| Archive stage designs and workingon by version | pending | historical designs and stage report |

## Acceptance Criteria

- The live benchmark script exposes repair cycles as a first-class reproducibility parameter.
- The rerun uses paid/live configured model keys and bounded limits.
- If the rerun reaches `ready`, the stage records that v0.2.33 used an underpowered experiment repair budget.
- If the rerun still fails, the stage records the next concrete Builder/runtime issue instead of closing E01.
- Active `docs/current-design/` and `docs/workingon/` are empty except README after archive.

## Current Status

Design 1 completed.

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

v0.2.33 event evidence shows repeated failed `test_run` calls and `RuntimeError: maximum repair cycles reached (1)`, so the first repair is to make the repair budget explicit and rerun.

Paid/live rerun evidence:

| Run | Build status | Benchmark | Draft | Notes |
| --- | --- | --- | --- | --- |
| v0.2.34 first | `needs_attention` | `0.3`, failed | `0` nodes | DeepSeek network error, not quality evidence |
| v0.2.34 retry | `ready` | `0.806`, failed | `8` nodes / `7` edges / `3` tests | readiness improved, but missing `context_assembler` |

Conclusion:

- The repair budget control is useful and should remain part of experiment evidence.
- Larger repair budget can make required mode reach `ready` on this complex case.
- E01 still cannot close because the ready graph missed `context_assembler`, so readiness and benchmark architecture coverage were not both satisfied.
