# experiment_v0.2.37_e04_local_repair_vs_full_rebuild_2026_07_09

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.37_e04_local_repair_vs_full_rebuild_2026_07_09.json`
- Status: `completed`
- Started: `2026-07-09T11:19:52.035644+00:00`
- Finished: `2026-07-09T11:20:49.302676+00:00`
- Question: For a localized failing draft, is local repair cheaper/faster/more reliable than full Builder rebuild from the same requirement?

## local_repair_arm

- `local_repair.status`: passed
- `local_repair.operation_count`: 1
- `local_repair.elapsed_seconds`: 0.015
- `before_test_report.passed`: false
- `before_test_report.summary`: passed=0, failed=1
- `after_test_report.passed`: true
- `after_test_report.summary`: passed=1, failed=0

## full_rebuild_arm

- `test_report.passed`: true
- `test_report.summary`: passed=1, failed=0
- `status`: completed
- `build_status`: published
- `elapsed_seconds`: 57.127

## comparison

- `local_success`: true
- `full_rebuild_success`: true
- `local_elapsed_seconds`: 0.015
- `full_rebuild_elapsed_seconds`: 57.127
- `local_operation_count`: 1
- `full_rebuild_model_calls`: 15
- `full_rebuild_tool_calls`: 22
- `narrow_conclusion`: Local repair is cheaper and sufficient for this localized template failure.

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
