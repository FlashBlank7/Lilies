# E02 true human panel execution package

Status: prepared_pending_external_execution

This package prepares the external human timing panel required to resolve the remaining E02 blocker. It does not contain participant results and must not be used to claim E02 completion.

## Files

- `participant_protocol.md`: participant flow, task order, roles, and timing rules.
- `timing_rubric.md`: timing definitions, success criteria, and scoring rubric.
- `consent_safety_notes.md`: consent, privacy, exclusion, and safety notes.
- `data_capture_schema.json`: machine-readable result schema.
- `blank_results.csv`: blank capture sheet matching the schema.
- `execution_checklist.md`: before/during/after checklist and completion gates.
- `packets/`: participant-facing raw/readable task packets, facilitator manifest, questionnaire, and facilitator-only answer key.

## Analyzer

- `scripts/e02_human_panel_analyzer.py` validates captured CSV rows and generates timing/correctness analysis.
- It requires paired `raw_json` and `readable_testframe` rows per participant and at least 5 paired participants before a timing claim can be supported.
- The bundled blank results sheet has zero rows and does not complete E02.

## Packets

- `packets/task_packet_raw_json.md` is the raw JSON evidence condition.
- `packets/task_packet_readable_testframe.md` is the readable TestFrame condition.
- `packets/facilitator_packet_manifest.md` defines counterbalanced A/B order and capture rules.
- `packets/answer_key.md` is facilitator-only and must not be shown to participants.

## Completion Boundary

E02 remains `completed_for_proxy_blocked_for_true_human_panel` until this package is executed with real recruited participants and the captured data is analyzed.
