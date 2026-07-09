# experiment_v0.2.39_e05_after_expandability_fix_2026_07_09

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.39_e05_after_expandability_fix_2026_07_09.json`
- Status: `completed`
- Started: `2026-07-09T12:01:56.241396+00:00`
- Finished: `2026-07-09T13:29:35.524735+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- |
| none | completed | needs_attention | 536.444 | 46/59 | suggest=1, expand=0 | passed=true, score=0.85 | builder stopped before mandatory tests passed |
| shallow | completed | needs_attention | 482.334 | 22/30 | suggest=1, expand=1 | passed=true, score=0.85 | DeepSeek request timed out |
| deep | completed | published | 147.504 | 23/32 | suggest=1, expand=1 | passed=true, score=0.85 |  |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
