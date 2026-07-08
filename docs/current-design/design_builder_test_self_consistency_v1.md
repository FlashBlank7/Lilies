# design_builder_test_self_consistency_v1

## 1. Goal

Make Builder-generated tests self-consistent with the current draft before they are persisted.

The v0.2.6 paid experiment showed a concrete failure: the Builder added a mandatory test whose `required_node_types` included `extract_text`, while the generated workflow only contained `start`, `model_turn`, `template_transform`, and `end`. Final validation failed after 36 model turns.

## 2. Module Boundary

Code:

- `platform/backend/src/agent_platform/builder.py`
- `tests/test_workflow.py`

No API model or storage schema change is planned.

## 3. Control Flow

```text
Builder calls test_add
  -> parse WorkflowTestCase
  -> inspect current draft node types and tool nodes
  -> if required_node_types/tool_nodes are missing:
       raise RuntimeError with actionable message
       do not persist the test
       emit failed build.operation tool_result
  -> else persist add_test as before
```

## 4. Implementation Plan

1. Add a small helper in `WorkflowBuilder` to compute draft node types and tool node names.
2. Call it in the `test_add` branch before `ApplicationService.apply_operation()`.
3. Include missing and available values in the error message.
4. Add a scripted provider test that tries to add a test requiring `extract_text` when only `start` and `end` exist.
5. Verify the invalid test is rejected and not stored.

## 5. Acceptance Criteria

- A bad `test_add` call produces a tool error containing `test required unavailable node types`.
- The invalid test does not appear in the draft.
- The Builder can still complete with an auto smoke test if no valid mandatory test exists.
- Full backend pytest passes.

## 6. Referenced Evidence

- `docs/stage-reports/v0.2.6_paid_builder_benchmark_experiment.md`
- `docs/workingon/experiment_paid_builder_benchmark_result_2026_07_09.json`

## 7. Implementation Result

Status: implemented.

Implemented code:

- `platform/backend/src/agent_platform/builder.py`
- `tests/test_workflow.py`

Guard behavior:

- Builder `test_add` now checks `required_node_types` and `required_tool_nodes` against the current draft before persisting.
- Missing requirements raise a `RuntimeError` tool error with actionable missing/available values.
- The bad test is not stored.

Regression evidence:

- `test_builder_rejects_tests_requiring_unavailable_node_types`

Verification:

- Focused regression passed: `1 passed`.
- Full backend tests passed: `56 passed, 1 warning`.
- Focused ruff passed.

Remaining risk:

- The same paid Builder benchmark has not yet been rerun after this fix.

