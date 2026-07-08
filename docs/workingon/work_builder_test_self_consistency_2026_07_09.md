# work_builder_test_self_consistency_2026_07_09

## Goal

在 Automatic Evolution Mode 下推进 `v0.2.7_builder_test_self_consistency`，修复 v0.2.6 付费实验暴露的问题：Builder 生成的测试要求了当前 draft 中不存在的节点类型，最终导致 build `needs_attention`。

## Scope

Included:

- Add an early Builder-side guard for `test_add`.
- Reject tests whose `required_node_types` or `required_tool_nodes` do not exist in the current draft.
- Return actionable tool feedback to the Builder Team.
- Add deterministic regression coverage for the `extract_text`-style failure.

Excluded:

- Changing manual/API draft editing semantics.
- Automatically inventing missing nodes for the Builder.
- Rerunning the paid benchmark in this stage unless the deterministic fix is complete and a small paid rerun is still bounded.

## Plan

| Step | Work | Status |
| --- | --- | --- |
| 1 | Analyze v0.2.6 failure and Builder tool path | completed |
| 2 | Create current design | completed |
| 3 | Implement Builder `test_add` self-consistency guard | completed |
| 4 | Add regression test | completed |
| 5 | Run verification | completed |
| 6 | Archive and commit stage | in progress |

## Linked Current Design

- `docs/current-design/design_builder_test_self_consistency_v1.md`

## Acceptance Criteria

- Invalid Builder `test_add` calls fail before persisting the test.
- Tool error names missing node/tool requirements and lists available draft options.
- Regression test proves invalid test is not stored.
- Existing backend tests still pass.

## Current Decision

Proceed to next design: yes. The guard is implemented, deterministic regression passes, and remaining risk is a paid rerun.

## Implementation Evidence

- Implemented Builder-side `test_add` guard in `platform/backend/src/agent_platform/builder.py`.
- Added `InvalidRequiredNodeTestBuilderProvider` regression fixture in `tests/test_workflow.py`.
- Added `test_builder_rejects_tests_requiring_unavailable_node_types`.

Behavior:

- If a Builder-generated test requires unavailable `required_node_types` or `required_tool_nodes`, the tool call fails before persisting.
- The error includes missing values and available draft values.
- The invalid test is not stored in the draft.
- Existing preflight smoke test can still supply a mandatory structural test when no valid mandatory test exists.

Verification:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_builder_rejects_tests_requiring_unavailable_node_types -q
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests/test_workflow.py scripts/live_builder_benchmark_suite.py
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check platform/backend/src/agent_platform/builder.py tests/test_workflow.py scripts/live_builder_benchmark_suite.py
```

Result:

- Focused regression: `1 passed`.
- Full backend tests: `56 passed, 1 warning`.
- Focused ruff: passed.

Paid/live boundary:

- No paid model rerun in this stage; this stage is the deterministic fix.
- Next stage should rerun the same paid Builder benchmark with a new result file and `.docx` report, without overwriting v0.2.6 evidence.
