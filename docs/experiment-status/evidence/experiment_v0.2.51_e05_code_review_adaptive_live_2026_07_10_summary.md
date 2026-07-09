# E05 template reuse-depth live comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.51_e05_code_review_adaptive_live_2026_07_10.json`
- Status: `completed`
- Started: `2026-07-09T20:05:27.533261+00:00`
- Finished: `2026-07-09T20:23:32.737125+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| shallow | completed | ready | 388.427 | 42/45 | suggest=1, expand=1 | case=true, score=0.85 |  |
| deep | completed | needs_attention | 382.777 | 44/70 | suggest=1, expand=1 | case=true, score=0.85 | runtime |
| adaptive | completed | published | 313.696 | 38/46 | suggest=1, expand=1 | case=true, score=0.85 |  |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
