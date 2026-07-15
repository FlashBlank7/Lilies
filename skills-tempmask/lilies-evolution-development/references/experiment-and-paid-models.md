# Experiment And Paid Model Governance

Use this reference for Lilies experiments, evidence application, live/paid validation, and experiment-status maintenance.

## Experiment Completion

An experiment is complete only when it has:

- question,
- setup/design,
- execution,
- result,
- conclusion,
- evidence chain,
- concise `.docx` report when it is a Lilies formal experiment,
- status index/ledger update.

Useful engineering progress is not experiment completion.

## Application Gate

Before using an experiment result to change code, prompts, benchmark semantics, Harness policy, Builder behavior, Template behavior, or workflow strategy:

1. Confirm the experiment is complete.
2. Confirm the report artifact exists.
3. Confirm the result is strong enough for the proposed engineering change.
4. If evidence is narrow, make a narrow fix for the observed failure.
5. If incomplete, finish the experiment, create a smaller bounded experiment, or label the change as a hypothesis requiring validation.
6. After engineering change, update the experiment ledger with applied marker, changed files/modules, stage report, tests, live evidence if relevant, and caveats.

Markers:

- `已应用`: experiment result guided engineering change.
- `验证应用`: experiment validated an already-made improvement.
- `未应用`: not used for engineering.
- `未关闭`: original experiment question remains open.

## Paid / Live Model Tests

Lilies targets industrial usefulness, not toy offline checks. Do not avoid configured paid model/API tests merely to save small cost when behavior depends on:

- model output,
- Builder Team quality,
- workflow generation quality,
- tool execution,
- live provider compatibility,
- benchmark validity,
- Platform Harness enforcement.

Default order:

1. Run focused deterministic tests first.
2. Run bounded live acceptance with configured provider/model when credentials are available.
3. Record provider, model, prompt/task, budget cap, command, evidence, result, failure mode, and approximate cost if visible.
4. Route resource-consuming live work through task monitor boundary when available, or document why not.
5. Store evidence in workingon, experiment-status summary, and formal report as appropriate.

Skip paid/live only when credentials/services are unavailable, user forbids paid calls, expected cost is material or unbounded, or safety/privacy/legal/data-loss risk is unacceptable. Record the skip reason and next command.

## Evidence Reading Policy

Experiment status is split for token efficiency:

- `docs/experiment-status/v*.md`: compact index and next action.
- `docs/experiment-status/ledgers/*.md`: single-experiment ledger.
- `docs/experiment-status/evidence/*_summary.md`: default raw evidence summary.
- `docs/experiment-status/evidence/*.json`: raw evidence for disputes/debugging only.

When creating or updating evidence summaries, include at minimum:

- arm,
- status/build status,
- calls,
- failure,
- benchmark,
- conclusion or reader guidance.

Use `scripts/summarize_experiment_evidence.py` to generate or refresh `*_summary.md` files in bulk. Default readers should open the summary first and escalate to raw JSON only for disputes or missing fields.

## Experiment Backlog Discipline

The original backlog is closed only by:

- completed experiment,
- applied experiment,
- explicit discard,
- replacement by a newer experiment,
- blocked/deferred with next action.

`部分实现`, deterministic tests, or code capability alone do not close an experiment.

At every stage archive, update the current experiment status index or relevant ledger if the stage creates, runs, applies, verifies, blocks, defers, or supersedes an experiment.
