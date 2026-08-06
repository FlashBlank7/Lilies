# E02 human panel packets

Status: prepared_pending_external_execution

These packet files provide the participant-facing materials required by the E02 true human panel protocol. They are grounded in the existing v0.2.36 E02 raw evidence and readable TestFrame evidence.

## Files

- `facilitator_packet_manifest.md`: session order, packet assignment, source evidence, and handling rules.
- `task_packet_raw_json.md`: participant packet for the raw JSON evidence condition.
- `task_packet_readable_testframe.md`: participant packet for the readable TestFrame condition.
- `post_task_questionnaire.md`: post-condition and final preference questions.
- `answer_key.md`: facilitator-only scoring key.

## Source Evidence

- Raw JSON evidence: `../../evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json`
- Summary: `../../evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09_summary.md`
- Prior proxy report: `../../reports/2026-07-09_2013_E02_readable_testframe_reviewer_proxy.docx`

## Completion Boundary

These packet files make the panel executable, but they do not contain participant rows. E02 remains `completed_for_proxy_blocked_for_true_human_panel` until recruited participants complete both conditions and the captured CSV is analyzed.
