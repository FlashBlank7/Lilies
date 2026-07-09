# Lilies Evolution Development Templates

## Working Task

```markdown
# work_<topic>

## 1. Goal

## 2. Scope

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |

## 4. Full Task Set Disposition

Source stage report:

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |

Every next-stage task must be listed. Do not start implementation if any task is missing from this table.

## 5. Evidence

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |

## 7. Review Before Archive

- Completion summary:
- Files changed:
- Verification:
- Remaining risk:
- All next-stage tasks dispositioned: yes/no
- All accepted tasks expanded into designs: yes/no
- Every accepted design completed or explicitly blocked/deferred: yes/no
- Engineering closure level claimed:
- Engineering closure actually achieved:
- Partial slices carried forward:
- Active current-design will be cleared after archive: yes/no
- Active workingon will be cleared after archive: yes/no
- Minor version target closure: completed / partial / blocked / deferred
- Experiment deliverables, if any:
- Awaiting user review before archive: yes

## 8. Archive Conditions

## 9. Automatic Evolution

- Automatic Evolution Mode active: yes/no
- Current version:
- Archive automatically after verification: yes/no
- Next version selection source:
- Continue after archive: yes/no
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

## 7. No Next-stage Authority

- This design expands one accepted task only.
- It must not select, rank, or guide next-stage work.
- It must not contain "next version" instructions.
- Next-stage guidance belongs in the stage report only.
```

## Experiment Record

```markdown
# experiment_<topic>

## 1. Question

## 2. Setup

## 3. Result

## 4. Decision Impact

## 5. Application Status

- Application marker: not applied / 已应用 / 验证应用
- Engineering change, if applied:
- Evidence chain:
- Report supplement updated: yes/no

## 6. Follow-up
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

- Current design status: completed / revise current design / blocked
- Evidence:
- Next-stage guidance: prohibited here; record it in the stage report only.
```

## Rapid Result Report

```markdown
# result_<topic>

## 1. Overall Verdict

## 2. Source Scope

- Target baseline or question:
- Documents read:
- Latest authoritative stage:

## 3. Item Status

| Item | Status | Evidence | Gap / Next Action |
| --- | --- | --- | --- |

## 4. Cross-cutting Findings

## 5. Recommended Next Actions

## 6. Confidence / Caveats
```

## Experiment Status Ledger

```markdown
# v0.x Experiment Status

更新时间:
当前最新 stage:

## 1. Overall Verdict

## 2. Original Backlog Status

| ID | Topic | Status | Evidence | Next Step |
| --- | --- | --- | --- | --- |

## 3. Completed Experiment Reports

| Experiment | Application Marker | Report | Raw Evidence | Engineering Application |
| --- | --- | --- | --- | --- |

## 4. Applied Evidence Chains

### <experiment> -> <engineering change>

- Application marker:
- Experiment stage:
- Result:
- Engineering change:
- Code anchors:
- Verification:
- Remaining caveat:

## 5. Current Version Open Items

## 6. Stage Archive Gate

- Stage created/changed experiments:
- Stage used experiment results for engineering:
- Ledger updated:
- Original backlog items closed:
- Items carried forward:
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

## 4. Minor Version Completion Gate

- Original version target:
- Full next-stage task set source:
- Full next-stage task set listed in workingon: yes/no
- Every task dispositioned as accepted/blocked/deferred/superseded: yes/no
- Stage report next-task set fully expanded into designs: yes/no
- Unexpanded next-stage tasks and reasons:
- Engineering closure level claimed: backend slice / vertical slice / platform boundary / product capability / research experiment
- Engineering closure actually achieved:
- Backend/code closure: yes/no
- UI/API closure: yes/no/not applicable
- Harness/operations closure: yes/no/not applicable
- Verification closure: yes/no
- Evidence closure: yes/no
- Completed as claimed: yes/no
- Only prerequisite completed: yes/no
- Experiment deliverables required: yes/no
- Experiment reports produced:
- Experiment status ledger updated: yes/no
- Experiments applied to engineering:
- Applied markers recorded: yes/no
- Experiment reports supplemented with evidence chain: yes/no
- Current-design acceptance fully met: yes/no
- Carry-forward reason, if any:

## 5. Next-stage Tasks

## 6. Intellectual Asset Candidates

| Candidate | Evidence Chain | Decision |
| --- | --- | --- |

## 7. Historical Designs

Archive readiness:

- Corresponding version/state document exists: yes/no
- Version or stage marker:
- If no, do not archive design files yet.

| Historical design path | Source current-design | Final status | Evidence |
| --- | --- | --- | --- |

## 8. Workingon Archive

| Archive path | Source path | Contents | Evidence |
| --- | --- | --- | --- |

Active `docs/workingon/` after archive:

- Only README remains: yes/no

Active `docs/current-design/` after archive:

- Only README remains: yes/no

## 9. Archive Commit

- Auto commit required: yes
- Staged paths verified with `git diff --cached --name-status`: yes/no
- Commit message:
- Commit hash:

## 10. Automatic Evolution Handoff

- Automatic Evolution Mode active: yes/no
- Continue to next version: yes/no
- Next version:
- Authoritative next task source: this stage report only
- Next-stage task set:
- Required first action in next version: expand the full next-stage task set into workingon and current-design files before implementation
- First workingon file to create/update:
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
