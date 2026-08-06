# E05 template reuse-depth live comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.43_e05_customer_support_2026_07_09.json`
- Status: `completed`
- Started: `2026-07-09T14:46:58.584210+00:00`
- Finished: `2026-07-09T15:22:50.675792+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| none | completed | published | 259.449 | 37/64 | suggest=1, expand=0 | passed=true, score=0.78 |  |
| shallow | completed | needs_attention | 247.175 | 7/13 | suggest=1, expand=1 | passed=true, score=0.85 | model_provider |
| deep | completed | needs_attention | 581.918 | 42/67 | suggest=1, expand=1 | passed=true, score=0.85 | runtime |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
