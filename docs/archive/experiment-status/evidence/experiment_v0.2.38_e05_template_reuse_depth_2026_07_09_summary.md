# experiment_v0.2.38_e05_template_reuse_depth_2026_07_09

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.38_e05_template_reuse_depth_2026_07_09.json`
- Status: `completed`
- Started: `2026-07-09T11:33:34.304877+00:00`
- Finished: `2026-07-09T11:50:40.800946+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| none | completed | published | 228.812 | 24/41 | suggest=1, expand=0 | passed=true, score=0.85 |  |
| shallow | completed | needs_attention | 385.149 | 42/73 | suggest=1, expand=2 | passed=true, score=0.733 | builder stopped before mandatory tests passed |
| deep | completed | needs_attention | 412.285 | 42/77 | suggest=1, expand=2 | passed=true, score=0.733 | builder stopped before mandatory tests passed |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
