# v0.4.4 Structured Loop Feedback

Status: active

Tasks: `V04-04-T01A`, `V04-04-T01E`

## Decision

Keep the outer workflow acyclic and make feedback an explicit Loop/Subflow contract. Extend Loop with initial state, a state-update reference, a feedback reference, optional cancel condition/value, named state/feedback inputs, checkpoints, and per-iteration events. Preserve the current fields and behavior for old drafts.

## Runtime Contract

- Each iteration receives `iteration`, `previous`, the named loop state, and the named prior feedback.
- The nested graph emits a new state and feedback through configured references.
- Stop and cancel are evaluated after a complete nested iteration; cancel returns a truthful cancelled loop result instead of pretending success.
- `loop.iteration.started` and `loop.iteration.completed` expose bounded state, feedback, continuation, and reason.
- Checkpoints preserve the iteration output, next state, and feedback needed to inspect or resume.

## Acceptance

A two-turn model/tool replay must prove that a real tool result reaches the second decision and that the final answer stops the Loop. A separate deterministic case must prove cancel semantics, and an old Loop fixture must continue to validate and execute.
