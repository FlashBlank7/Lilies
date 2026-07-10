# E02 answer key

Status: facilitator_only

Do not show this file to participants.

## Expected Findings

| Finding | Expected repair target | Expected first repair action |
| --- | --- | --- |
| Outline does not include the required magic system setting constraint | `template_transform` or `model_turn prompt` | Inspect the generation prompt/template and restore the missing magic system constraint. |
| Context assembly is not visible as an inspectable workflow surface | `missing context_assembler node` | Add a `context_assembler` node before the generation step instead of hiding context assembly inside a direct prompt. |

## Correctness Guidance

Mark `localization_correct=true` when the participant identifies the missing magic-system constraint and/or the missing inspectable context assembly surface with a repair target close enough for an engineer to act.

Mark `recommendation_actionable=true` when the recommendation names a concrete implementation action, not just "fix the output" or "rerun the workflow".

## Boundary

This key is for scoring participant rows. It is not participant evidence and cannot close E02 without real session rows.
