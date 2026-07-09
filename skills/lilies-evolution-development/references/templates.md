# Lilies Compact Templates

Use these templates to keep document evolution dense and traceable.

## Working Task

```markdown
# work_<topic>

## Goal

## Source

- Stage report:
- Version:

## Full Task Set

| Task | Disposition | Design | Reason |
| --- | --- | --- | --- |

## Execution Status

| Design | Status | Evidence | Next action |
| --- | --- | --- | --- |

## Acceptance

- All tasks dispositioned:
- All accepted designs completed/blocked/deferred:
- Verification:
- Experiment status updated:
- Archive ready:
```

## Current Design

```markdown
# design_<component-or-flow>

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

## Implementation Evidence

```markdown
# implementation_<topic>

## Changes

## Files

## Verification

## Live / Paid Acceptance

- Required:
- Provider/model:
- Budget:
- Command:
- Result:
- Skip reason:

## Remaining Risk

## Design Decision

- Continue current design / revise current design / proceed to next design / blocked:
```

## Compact Stage Report Factsheet

```markdown
# v0.x.y_<topic>

## Goal

## Completed

| Item | Status | Evidence |
| --- | --- | --- |

## Verification

| Check | Result | Evidence |
| --- | --- | --- |

## Unfinished / Carried Forward

| Item | Reason | Next action |
| --- | --- | --- |

## Historical Designs

| Historical design | Final status | Evidence |
| --- | --- | --- |

## Workingon Archive

| Archive | Contents |
| --- | --- |

## Next-stage Tasks

| Task | Why now | Closure target |
| --- | --- | --- |

## Archive Commit

- Commit:
- Active current-design clean:
- Active workingon clean:

## Automatic Evolution Handoff

- Continue:
- Next version:
- First workingon:
```

Rule: stage report is the next-stage authority, but it stays compact. Command detail belongs in workingon archive.

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
