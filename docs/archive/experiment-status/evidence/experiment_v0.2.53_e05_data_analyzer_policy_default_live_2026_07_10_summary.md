# E05 template reuse-depth live comparison

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.53_e05_data_analyzer_policy_default_live_2026_07_10.json`
- Status: `completed`
- Started: `2026-07-09T21:20:14.073434+00:00`
- Finished: `2026-07-09T22:19:48.217861+00:00`
- Provider/model: `multi` / `deepseek-v4-pro`

## Arms

| Arm | Status | Build | Elapsed | Calls | Template | Reuse | Benchmark | Failure |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| adaptive | completed | published | 379.655 | 22/28 | suggest=1, expand=1 | explicit, adaptive->deep, compose_modules | case=true, score=0.85 |  |
| policy_default | completed | needs_attention | 186.714 | 2/3 | suggest=1, expand=0 | policy_default, adaptive->deep, compose_modules | case=false, score=0.5 | model_provider |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
