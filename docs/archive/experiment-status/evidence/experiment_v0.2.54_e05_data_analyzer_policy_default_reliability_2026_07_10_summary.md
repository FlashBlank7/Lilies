# E05 template reuse-depth live comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.54_e05_data_analyzer_policy_default_reliability_2026_07_10.json`
- Status: `completed`
- Started: `2026-07-09T22:43:32.804769+00:00`
- Finished: `2026-07-09T22:48:55.266261+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Reuse | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| policy_default | completed | published | 322.364 | 24/31 | suggest=1, expand=1 | policy_default, adaptive->deep, compose_modules | case=true, score=0.85 |  |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
