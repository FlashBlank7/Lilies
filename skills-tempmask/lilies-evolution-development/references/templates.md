# Lilies Compact Templates

Use these templates to keep document evolution dense and traceable.

## Optional Workingon Evidence

```markdown
# implementation_<topic>

## Source

- Source stage report:
- Source stage task:
- Current design:

## Changes

## Evidence / Intermediate Results

## Verification

## Remaining Risk

## Design Decision

- Continue current design / revise current design / proceed to next design / blocked:
```

Rule: workingon is optional and only stores intermediate evidence, implementation notes, experiments, and trace material. It is not the task-decomposition authority and must not decide the next version.

## Current Design

```markdown
# design_<component-or-flow>

## Source Stage Task

- Stage report:
- Task:

## Problem

## Boundary

## Solution

## Implementation Plan

## Acceptance

## Evidence Required

## Final Status

- Pending / implemented / revised / blocked:
```

Rule: current design never guides the next stage. It only expands one contracted task. A blocked mandatory design keeps the stage open; only optional work may be deferred through the stage report.

## Mandatory Stage Report

Always instantiate the canonical repository template at `docs/stage-reports/STAGE_REPORT_TEMPLATE.md`. Its v2 contract requires, at minimum:

1. `Stage Identity` with template version, Program Charter, source report, closure level, and serious version-size justification.
2. `Source Task Set` with stable task IDs and source intent IDs.
3. A locked `Stage Contract` separating mandatory and optional tasks, with measurable acceptance criteria, required evidence, a contract-lock SHA-256 fingerprint, and a Git baseline commit.
4. `Completed Work`, `Verification`, and a fresh-context `Closure Audit` mapped back to every mandatory task ID.
5. `Deviations`, `Unresolved / Blocked / Deferred`, and `Intent Coverage`; mandatory unresolved work prevents archive.
6. `Evidence Debt` for desired evidence above the contracted closure floor: achieved level, unavailable environment, claim ceiling, owner, and recheck trigger. Evidence debt cannot hide missing mandatory behavior.
7. Product/experiment updates, historical-design recycling, and workingon archive records.
8. `Next-stage Task Set` as the only next-task sequencing authority beneath the campaign objective, again using stable task IDs and source intent IDs.
9. `Automatic Evolution Handoff` with `Current task ID`, `Next version`, `First task ID`, and `Resume from stage report`.

Rule: a stage report is the next-stage authority. If a section has no content, write an explicit `none` row instead of omitting it. Workingon stores evidence only. Before archive, run both `scripts/validate_stage_report_template.py` and `scripts/validate_evolution_control.py`; a passing closure audit is mandatory.

## Historical Design Final Contract

```markdown
# v0.x.y_design_<topic>_v<n>

## Source

- Stage:
- Original design:
- Final status:

## Problem

## Boundary

## Final Design

## Acceptance

## Evidence

## Remaining Risk
```

Rule: historical design keeps the final contract only. Do not repeat experiment results, command transcripts, or stage summaries.

## Experiment Ledger

```markdown
# E##_topic Ledger

状态:

## Current Conclusion

## Evidence

| Item | Path |
| --- | --- |

## Application Marker

- Not applied / 已应用 / 验证应用:
- Engineering change:
- Stage:

## Boundary

## Next Step
```

## Rapid Result Report

```markdown
# result_<topic>

## Verdict

## Source Scope

## Item Status

| Item | Status | Evidence | Gap / Next |
| --- | --- | --- | --- |

## Cross-cutting Findings

## Next Actions

## Caveats
```

## Docs-only Rollback Plan

```markdown
# docs_rollback_<target>

## Target

- Baseline:
- Documentation-only scope confirmed:

## Delete

| Path | Reason |
| --- | --- |

## Restore

| Path | Source |
| --- | --- |

## Keep

| Path | Reason |
| --- | --- |

## Verify

- `find <dirs> -maxdepth 3 -type f | sort`
- `git diff --name-status <baseline> -- <paths>`
- `git status --short`

## Result
```
