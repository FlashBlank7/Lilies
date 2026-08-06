# E02 timing rubric

Status: prepared_pending_external_execution

## Primary Metric

`time_to_actionable_review_seconds`

The elapsed seconds from first reading the packet to submitting an answer that includes:

- the suspected failing path or component,
- a concrete explanation of the issue,
- a repair recommendation.

## Secondary Metrics

- `localization_correct`: whether the participant identified the intended issue location.
- `recommendation_actionable`: whether the recommendation could guide an implementation fix.
- `confidence_1_to_5`: participant self-rated confidence.
- `facilitator_intervention_count`: number of clarifying interventions.
- `preference`: raw, readable, or no preference.

## Scoring

| Field | Pass Criteria |
| --- | --- |
| `localization_correct` | The answer identifies the expected path, node, assertion, or evidence block. |
| `recommendation_actionable` | The recommendation names an implementable change or next diagnostic step. |
| `completed` | Participant submits a final answer without abandoning the packet. |

## Analysis Plan

1. Validate that each participant has one raw and one readable row.
2. Exclude rows marked `completed=false` from timing comparison, but report exclusions.
3. Compare median time for raw vs readable.
4. Compare localization correctness and actionable recommendation rates.
5. Report confidence distribution and facilitator interventions.
6. Only claim E02 human timing improvement if readable has lower median time and does not reduce correctness.

## No-Claim Boundary

If participants are not recruited or rows are not captured, the result remains `prepared_pending_external_execution`, not completed.
