# E02 participant protocol

Status: prepared_pending_external_execution

## Objective

Measure whether the readable TestFrame report reduces true human review time and improves issue localization compared with raw JSON evidence.

## Participants

- Minimum participants: 5
- Recommended participants: 8 to 12
- Profile: engineers or technically capable reviewers who can inspect workflow/test evidence.
- Exclusion: participants who authored the specific E02 artifacts or already know the expected answer.

## Materials

- Raw evidence packet: `packets/task_packet_raw_json.md`
- Readable TestFrame packet: `packets/task_packet_readable_testframe.md`
- Task prompt and output fields embedded in each packet
- Timing capture sheet: `blank_results.csv`
- Post-task confidence questions: `packets/post_task_questionnaire.md`
- Facilitator manifest: `packets/facilitator_packet_manifest.md`
- Facilitator-only answer key: `packets/answer_key.md`

## Design

Use a within-subject counterbalanced design.

- Group A reviews raw evidence first, then readable TestFrame.
- Group B reviews readable TestFrame first, then raw evidence.
- Each participant reviews matched tasks with equivalent expected findings.
- The facilitator must not reveal which surface is expected to perform better.

## Procedure

1. Confirm participant consent and eligibility.
2. Assign participant id and group.
3. Present the first packet and start timer when the participant begins reading.
4. Stop timer when the participant submits issue localization and repair recommendation.
5. Record correctness, confidence, notes, and any facilitator intervention.
6. Give a short reset interval.
7. Repeat with the second packet.
8. Ask the post-task preference question.
9. Store results in the capture sheet without personal identifiers.

## Timing Rules

- Start time: first moment participant begins reading task material.
- Stop time: participant submits final answer for that packet.
- Pauses: only record facilitator or environment interruptions; do not subtract unless predeclared.
- Abandon: mark `completed=false` and record reason.

## Completion Requirement

This protocol only resolves E02 after real participant rows are collected and analyzed. Dry runs, proxy model reviews, or facilitator-only walkthroughs do not resolve the blocker.
