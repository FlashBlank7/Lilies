# E05 template reuse-depth live comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.45_e05_customer_support_rerun_2026_07_10.json`
- Status: `completed`
- Started: `2026-07-09T15:39:29.810261+00:00`
- Finished: `2026-07-09T16:01:57.097585+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| none | completed | published | 198.961 | 25/67 | suggest=1, expand=0 | case=true, score=0.85 |  |
| shallow | completed | ready | 545.849 | 42/54 | suggest=1, expand=1 | case=true, score=0.85 |  |
| deep | completed | needs_attention | 602.071 | 37/56 | suggest=1, expand=1 | case=true, score=0.85 | build_timeout |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
