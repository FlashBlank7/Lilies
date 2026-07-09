# experiment_v0.2.36_e02_readable_testframe_review_2026_07_09

## Summary

- Raw evidence: `docs/experiment-status/evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json`
- Status: `completed`
- Started: `2026-07-09T11:12:59.580965+00:00`
- Finished: `2026-07-09T11:13:06.347499+00:00`
- Question: Does readable TestFrame/report output improve reviewer comprehension and repair targeting compared with raw JSON-style test output?

## Paid Reviewer Proxy

| Condition | Model | Duration | Score | Matched | Main failure target |
| --- | --- | --- | --- | --- | --- |
| raw_legacy_json | deepseek-v4-pro | 3.164 | 0.375 | 3/8 | outline magic system mention |
| readable_testframe | deepseek-v4-pro | 3.533 | 1 | 8/8 | template_transform or model_turn prompt |

## Reader Guidance

Use this summary for routine stage/ledger reads. Open the raw JSON only when debugging a disputed result, missing field, or exact event trace.
