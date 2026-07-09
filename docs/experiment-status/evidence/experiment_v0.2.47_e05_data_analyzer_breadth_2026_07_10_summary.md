# E05 template reuse-depth live comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.47_e05_data_analyzer_breadth_2026_07_10.json`
- Status: `completed`
- Started: `2026-07-09T18:22:11.552536+00:00`
- Finished: `2026-07-09T19:19:16.728688+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| none | completed | needs_attention | 395.359 | 42/74 | suggest=1, expand=0 | case=false, score=0.71 | runtime |
| shallow | completed | needs_attention | 600.247 | 22/36 | suggest=1, expand=1 | case=true, score=0.85 | build_timeout |
| deep | completed | published | 461.068 | 21/32 | suggest=1, expand=1 | case=true, score=0.85 |  |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
