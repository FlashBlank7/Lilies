# work_v0.2.50_builder_deadline_visibility

## Goal

Turn build deadline from a mostly internal experiment/control field into a user-visible Builder setting and status surface.

## Source

- Stage report: `docs/stage-report-archives/v0.2.x/v0.2.49_adaptive_policy_live_validation.md`
- Version: `v0.2.50`

## Full Task Set

| Task | Disposition | Design | Reason |
| --- | --- | --- | --- |
| Normalize build deadline fields in Builder API responses | accepted | `design_builder_deadline_api_contract.md` | The API should make deadline state explicit instead of relying on callers to know the raw storage row shape. |
| Expose build deadline input + status in Studio build panel | accepted | `design_builder_deadline_studio_surface.md` | Deadline now changes real convergence behavior, so operators need to set it and see it. |
| Define or test adaptive defaultization threshold | deferred | none | Important, but separate from the narrow deadline visibility slice. |
| Keep E08 sidecar/passmode as a separate lane | deferred | none | Independent Harness experiment track. |

## Execution Status

| Design | Status | Evidence | Next action |
| --- | --- | --- | --- |
| `design_builder_deadline_api_contract.md` | completed | `platform/backend/src/agent_platform/api.py`; `tests/test_workflow.py`; `docs/workingon/implementation_v0.2.50_builder_deadline_visibility.md` | proceed to archive |
| `design_builder_deadline_studio_surface.md` | completed | `platform/frontend/app/applications/[id]/page.tsx`; `platform/frontend/lib/i18n.ts`; `docs/workingon/implementation_v0.2.50_builder_deadline_visibility.md` | proceed to archive |

## Acceptance

- All tasks dispositioned: yes
- All accepted designs completed/blocked/deferred: yes
- Verification: backend focused pytest passed; frontend TypeScript check passed
- Experiment status updated: not required for this stage
- Archive ready: yes
