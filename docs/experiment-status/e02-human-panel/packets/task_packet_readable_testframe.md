# E02 task packet: readable TestFrame

Packet type: `readable_testframe`

Status: prepared_pending_external_execution

## Task Prompt

Identify the failed requirement or workflow surface, choose the most likely repair target, and write the first repair action you would recommend to an engineer. Submit only when the answer is actionable enough for another engineer to begin repair.

## Output Fields

- Failed requirement or surface:
- Most likely repair target:
- First repair action:
- Confidence from 1 to 5:

## Source

- Evidence file: `../../evidence/experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json`
- Packet path inside evidence: `packets.readable_testframe`
- Condition id for capture sheet: `readable_testframe`
- Task id for capture sheet: `e02_readable_testframe_v0.2.36`

## Evidence Excerpt

### Outline and setting adherence

- Category: `content`
- Purpose: Check whether the generated novel outline follows the required setting contract.
- Status: `failed`
- Mandatory: `true`
- Reviewer guidance: Review this before style polishing; a failure means the generation prompt or template lost key setting constraints.
- Reference: E02 fixture requirement: outline must mention the magic system.
- Failure target: `template_transform or model_turn prompt`
- Failed assertion: output at `outline` does not contain `magic system`.
- Feedback hints:
  - Inspect the generation prompt for missing setting constraints.
  - Check whether the final template dropped the user's required setting.

### Visible context assembly gate

- Category: `structure`
- Purpose: Check whether the BlockFlow exposes context assembly instead of hiding it inside one black-box prompt.
- Status: `failed`
- Mandatory: `true`
- Reviewer guidance: A failure points to workflow architecture, not content quality.
- Reference: E02 fixture requirement: context assembly must be inspectable.
- Failure target: `missing context_assembler node`
- Failed assertion: output path `assembled_context` does not exist.
- Feedback hints:
  - Add a context_assembler node before the LLM or template stage.
  - Do not rely on direct prompt concatenation for context assembly.
