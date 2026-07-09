# E05 template reuse-depth live comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.49_e05_data_analyzer_adaptive_live_2026_07_10.json`
- Status: `completed`
- Started: `2026-07-09T19:40:57.516020+00:00`
- Finished: `2026-07-09T19:52:12.558321+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| shallow | completed | published | 213.959 | 9/20 | suggest=1, expand=1 | case=true, score=0.85 |  |
| deep | completed | published | 301.29 | 15/23 | suggest=1, expand=1 | case=true, score=0.85 |  |
| adaptive | completed | published | 159.669 | 11/17 | suggest=1, expand=1 | case=true, score=0.85 |  |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
