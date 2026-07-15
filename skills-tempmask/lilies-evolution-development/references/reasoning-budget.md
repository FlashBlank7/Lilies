# Reasoning Budget Policy

Use this reference when a Lilies task is long, model-sensitive, or running Automatic Evolution Mode.

## Boundary

Codex may not be able to switch the host model or its internal reasoning mode from inside a turn. When direct switching is unavailable, treat this policy as work allocation guidance: spend more scrutiny on stage selection, closure, experiment interpretation, and risky edits; use compact routines for mechanical updates.

## Effort Allocation

Use high/deep scrutiny for:

- selecting the next version from a stage report,
- deciding whether a version is genuinely complete,
- interpreting paid/live experiment results,
- applying experiment results to engineering,
- Platform Harness enforcement boundaries,
- archive/rollback path selection,
- merge or dirty worktree risk.

Use medium scrutiny for:

- writing current-design contracts,
- implementing focused code changes,
- updating ledgers and stage reports,
- reviewing deterministic test failures.

Use light/fast routines for:

- regenerating evidence summaries,
- moving files during an already-decided archive,
- updating path inventories,
- formatting compact factsheets.

## Paid Model Budget

Paid model usage should be bounded, not avoided. Before live runs, record:

- objective,
- provider/model,
- max turns / max repair cycles / timeout / run count,
- expected evidence path,
- stop condition.

After live runs, record result and whether it changes engineering. If a run fails from provider timeout or budget, classify it as a boundary result, not necessarily a quality result.

## Token Conservation

Default reading order:

1. stage report factsheet,
2. experiment index,
3. single ledger,
4. summary evidence,
5. raw JSON only if needed,
6. workingon archive only for command details or unresolved disputes.

Do not copy raw JSON or command transcripts into stage reports or historical designs.
