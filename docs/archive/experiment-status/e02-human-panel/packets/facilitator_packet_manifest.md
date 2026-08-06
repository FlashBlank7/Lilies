# E02 facilitator packet manifest

Status: prepared_pending_external_execution

## Session Inputs

- Protocol: `../participant_protocol.md`
- Timing rubric: `../timing_rubric.md`
- Capture sheet: `../blank_results.csv`
- Analyzer: `../../../../scripts/e02_human_panel_analyzer.py`
- Raw packet: `task_packet_raw_json.md`
- Readable packet: `task_packet_readable_testframe.md`
- Facilitator-only key: `answer_key.md`

## Counterbalanced Order

| Group | First packet | Reset | Second packet |
| --- | --- | --- | --- |
| A | `raw_json` | 2 to 5 minutes | `readable_testframe` |
| B | `readable_testframe` | 2 to 5 minutes | `raw_json` |

Use alternating participant ids to keep the order balanced unless scheduling requires a predeclared block randomization.

## Participant Task Prompt

For each packet, identify the failed requirement or workflow surface, choose the most likely repair target, and write the first repair action you would recommend to an engineer. Submit only when the answer is actionable enough for another engineer to begin repair.

## Required Capture Fields

Record one CSV row per participant per condition:

- `participant_id`
- `group`
- `packet_type`
- `task_id`
- `started_at`
- `ended_at`
- `time_to_actionable_review_seconds`
- `completed`
- `localization_correct`
- `recommendation_actionable`
- `confidence_1_to_5`
- `facilitator_intervention_count`
- `preference`
- `notes`

## Handling Rules

- Do not show `answer_key.md` to participants.
- Do not reveal that readable TestFrame is expected to be easier.
- Do not subtract pauses unless the pause reason is recorded before analysis.
- Do not fill rows for dry runs, facilitator walkthroughs, or model proxy reviews.
- Do not change E02 status before the analyzer reports at least 5 paired participants.
