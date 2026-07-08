# Lilies Evolution Development Templates

## Working Task

```markdown
# work_<topic>

## 1. Goal

## 2. Scope

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |

## 4. Evidence

## 5. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |

## 6. Review Before Archive

- Completion summary:
- Files changed:
- Verification:
- Remaining risk:
- Awaiting user review before archive: yes

## 7. Archive Conditions
```

## Current Design

```markdown
# design_<component-or-flow>

## 1. Goal

## 2. Module Boundary

## 3. Data Flow / Control Flow

## 4. Implementation Plan

## 5. Acceptance Criteria

## 6. Referenced Intellectual Assets
```

## Experiment Record

```markdown
# experiment_<topic>

## 1. Question

## 2. Setup

## 3. Result

## 4. Decision Impact

## 5. Follow-up
```

## Implementation Evidence

```markdown
# implementation_<topic>

## 1. Implemented Changes

## 2. Files / Modules

## 3. Verification

## 4. Remaining Risk

## 5. Live / Paid Model Acceptance

- Required: yes/no
- Provider/model:
- Budget boundary:
- Command or endpoint:
- Result:
- Skip reason, if skipped:

## 6. Next Design Decision

- Decision: continue current design / revise current design / update workingon direction / proceed to next design
- Reason:
- Next action:
```

## Stage Report

```markdown
# YYYY-MM-DD_stage_<topic>

## 1. Stage Goal

## 2. Completed Items

| Item | Evidence | Status |
| --- | --- | --- |

## 3. Incomplete Items

| Item | Reason | Next Step |
| --- | --- | --- |

## 4. Next-stage Tasks

## 5. Intellectual Asset Candidates

| Candidate | Evidence Chain | Decision |
| --- | --- | --- |

## 6. Historical Designs

Archive readiness:

- Corresponding version/state document exists: yes/no
- Version or stage marker:
- If no, do not archive design files yet.

| Historical design path | Source current-design | Final status | Evidence |
| --- | --- | --- | --- |

## 7. Archive Commit

- Auto commit required: yes
- Staged paths verified with `git diff --cached --name-status`: yes/no
- Commit message:
- Commit hash:
```

## Historical Design

```markdown
# v0.x.y_design_<topic>

## 1. Source

- Version/state:
- Stage report or accepted state document:
- Original path:
- Archive reason:

## 2. Design Status

- Implemented / revised / deferred / rejected:
- Reason:

## 3. Final Design Summary

## 4. Implementation Evidence

## 5. Verification

## 6. Remaining Risk

## 7. Links
```

## Phase Report

```markdown
# YYYY-MM-DD_phase_<theme>

## 1. Phase Goal

## 2. Included Stages

## 3. Mainline Change

## 4. Architecture / Business / Process Evolution

## 5. Completed Assets

## 6. Residual Risks

## 7. Next Phase Direction
```

## Intellectual Asset

```markdown
# asset_<stable-topic>

## 1. Core Conclusion

## 2. Acquisition Cost

## 3. Evidence Chain

## 4. Applicability Boundary

## 5. Reuse Method

## 6. Misuse Cases
```

## Docs-Only Rollback Plan

```markdown
# docs_rollback_<target-stage-or-baseline>

## 1. Target Baseline

- Baseline commit or stage report:
- Reason for rollback:
- Documentation-only scope confirmed: yes/no
- Explicitly out of scope: source code, database migrations, dependency lockfiles, generated runtime artifacts, local caches, live application state

## 2. Delete List

| Path | Reason |
| --- | --- |

## 3. Restore List

| Path | Restore source |
| --- | --- |

## 4. Keep List

| Path | Reason |
| --- | --- |

## 5. Verification Commands

- `find <affected-dirs> -maxdepth 3 -type f | sort`
- `git diff --name-status <baseline> -- <affected-paths>`
- `git status --short`

## 6. Risks

## 7. Rollback Result
```
