# E02 execution checklist

Status: prepared_pending_external_execution

## Before Recruiting

- Confirm raw and readable packets are matched for difficulty.
- Confirm packets contain no secrets or personal data.
- Confirm facilitator can explain the task without leading the participant.
- Prepare participant ids and counterbalance assignment.
- Prepare a copy of `blank_results.csv`.

## During Each Session

- Read consent script.
- Confirm participant may stop at any time.
- Record participant id and group.
- Start and stop timer according to `timing_rubric.md`.
- Record answer, confidence, interventions, and notes.
- Avoid hints about expected findings.

## After Sessions

- Validate one raw and one readable row per participant.
- Check required fields against `data_capture_schema.json`.
- Calculate median time and correctness/actionability rates.
- Record exclusions and facilitator interventions.
- Write an analysis summary before changing E02 status.

## Completion Gates

E02 can only move beyond `completed_for_proxy_blocked_for_true_human_panel` if:

- at least 5 real participants are captured,
- each included participant has raw and readable rows,
- analysis summary exists,
- readable TestFrame does not reduce correctness,
- timing claim is based on human rows, not proxy or dry-run rows.

Until then, global completion must remain unclaimed.
