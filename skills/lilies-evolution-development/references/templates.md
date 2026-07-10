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

- Pending / implemented / revised / blocked / deferred:
```

Rule: current design never guides the next stage. It only expands one accepted task.

## Mandatory Stage Report

```markdown
# v0.x.y_<topic>

## Stage Identity

| Field | Value |
| --- | --- |
| Version | `v0.x.y` |
| Source stage report | `docs/stage-reports/<previous>.md` |
| Stage type | product / experiment / process / architecture / report / repair |
| Closure level | backend slice / vertical slice / platform boundary / product capability / research experiment / process architecture |
| Stage scope justification | Explain why this is a serious version-sized unit. If only one design is archived, justify the exception explicitly. |

## Source Task Set

| Source task from previous stage report | Disposition in this stage | Design / evidence | Reason |
| --- | --- | --- | --- |
| none | none | none | none |

## Goal

## Completed Work

| Item | Status | Evidence |
| --- | --- | --- |
| none | none | none |

## Verification

| Check | Result | Evidence |
| --- | --- | --- |
| none | none | none |

## Unresolved / Blocked / Deferred

| Item | Status | Reason | Next action |
| --- | --- | --- | --- |
| none | none | none | none |

## Experiment / Product Status Updates

| Ledger / surface | Update | Evidence |
| --- | --- | --- |
| none | none | none |

## Historical Designs

| Historical design | Final status | Evidence |
| --- | --- | --- |
| none | none | none |

## Workingon Archive

| Archive | Contents |
| --- | --- |
| none | none |

## Next-stage Task Set

| Task | Why now | Closure target |
| --- | --- | --- |
| none | none | none |

## Archive Commit

- Commit:
- Active current-design clean:
- Active workingon clean:

## Automatic Evolution Handoff

- Continue:
- Next version:
- First workingon:
```

Rule: stage report is the next-stage authority, and these sections are mandatory. If a section has no content, write an explicit `none` row instead of omitting it. Command detail belongs in workingon archive. The canonical repo template is `docs/stage-reports/STAGE_REPORT_TEMPLATE.md`; validate new reports with `scripts/validate_stage_report_template.py` when possible.

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
