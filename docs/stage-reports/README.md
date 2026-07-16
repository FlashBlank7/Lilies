# Active Stage Reports

This directory contains only the current major phase's active small-version reports plus the canonical template.

## Current State

| Field | Value |
| --- | --- |
| Active phase | `v0.4.x` |
| Program charter | `docs/evolution-control/PROGRAM_CHARTER.md` |
| Intent registry | `docs/evolution-control/report_intents.json` |
| Current stage | `docs/stage-reports/v0.4.8_evaluation_harness_profiles_and_environments.md` |
| Current task authority | The current stage report's locked `Stage Contract` and, after closure, its `Next-stage Task Set` |
| Previous phase archive | `docs/stage-report-archives/v0.3.x/` |
| Previous phase report | `docs/phase-reports/v0.3.0_product_usability_buffer_closeout.md` |

Active v0.4 reports currently include v0.4.0 through the active v0.4.8 Evaluation Harness vertical. Historical v0.2 and v0.3 reports are not active task sources; v0.4.8 was accepted only from the closed v0.4.7 report's `V04-08-T01` handoff.

## Rules

- Start or resume from the latest validator-valid v2 stage report and its stable current task ID.
- `Next-stage Task Set` is the only next-task authority. Program Charter and intent registry constrain coverage but do not choose work.
- Workingon stores intermediate evidence only.
- A mandatory task cannot be deferred, reclassified, or weakened by the implementing agent.
- Version archive requires a passing fresh-context Closure Audit plus `scripts/validate_stage_report_template.py` and `scripts/validate_evolution_control.py`.
- Major-version completion requires a phase report, a complete versioned stage-report archive, index repair, and an unresolved-intent handoff.
