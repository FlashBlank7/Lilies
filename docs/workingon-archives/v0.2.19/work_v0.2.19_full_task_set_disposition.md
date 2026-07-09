# work_v0.2.19_full_task_set_disposition

## 1. Goal

Run the first Automatic Evolution stage after `v0.2.18` by consuming the full next-stage task set, not by choosing a single convenient task.

This stage focuses on a frontend/product vertical slice that can be completed in one small version:

- Natural-language Draft Patch preview UI.
- Builder benchmark history UI.
- Platform Harness closure audit documentation for future hard-boundary work.

## 2. Scope

Included:

- Add preview-confirm-apply UI for deterministic natural-language draft patch preview.
- Add benchmark history visibility in Studio.
- Add a closure audit artifact that keeps Platform Harness unfinished hard-boundary work explicit.
- Maintain experiment status and archive active design/workingon after completion.

Not included in this version:

- Worker lease execution queue.
- Secret reference injection runtime.
- Stdio MCP sandbox/container egress enforcement.
- Formal E01/E02/E04/E05/E08 paid experiment suite.

Those items are carried forward with explicit disposition below.

## 3. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.18_evolution_governance_and_workspace_archive.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Natural-language Draft Patch preview UI | accepted | `docs/current-design/design_nl_draft_patch_preview_ui_v1.md` | Backend preview endpoint exists; a product UI can close the preview-confirm-apply slice. |
| Worker lease and durable execution semantics | deferred | none | Requires platform execution model changes and should not be hidden inside a frontend stage. Carry to `v0.2.20`. |
| Benchmark history UI | accepted | `docs/current-design/design_builder_benchmark_history_ui_v1.md` | Backend history endpoint exists and can be made inspectable in Studio. |
| Secret store and secret reference injection | deferred | none | Requires secret reference data model and runtime injection boundary. Carry to a dedicated Platform Harness stage. |
| Sandbox/container egress controls for stdio tools | deferred | none | Requires process/container policy design. Carry to a dedicated Platform Harness stage. |
| Original v0.2 experiment backlog closure | deferred | none | Needs formal experiment design and DOCX reports. Carry after UI and audit surfaces are stable. |
| Platform Harness closure audit | accepted | `docs/current-design/design_platform_harness_closure_audit_v1.md` | Needed to stop partial Platform Harness slices from being described as full closure. |

All next-stage tasks listed: yes.

## 4. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| NL draft patch preview UI | `docs/current-design/design_nl_draft_patch_preview_ui_v1.md` | completed | User can enter instruction, preview operations/warnings, then explicitly apply operations. |
| Builder benchmark history UI | `docs/current-design/design_builder_benchmark_history_ui_v1.md` | completed | Studio can fetch and display benchmark history records from the existing endpoint. |
| Platform Harness closure audit | `docs/current-design/design_platform_harness_closure_audit_v1.md` | completed | Audit artifact lists completed slices, missing hard-boundary work, and next concrete tasks. |

## 5. Evidence

- Frontend implementation:
  - `platform/frontend/app/applications/[id]/page.tsx`
  - `platform/frontend/lib/platform.ts`
  - `platform/frontend/lib/i18n.ts`
  - `platform/frontend/app/globals.css`
- Platform Harness closure audit:
  - `docs/workingon-archives/v0.2.19/result_platform_harness_closure_audit_v0.2.19.md`
- Deterministic backend verification:
  - `.venv/bin/python -m pytest tests/test_workflow.py::test_natural_language_draft_patch_preview_is_non_destructive tests/test_workflow.py::test_builder_benchmark_history_survives_app_recreation -q`
  - Result: `2 passed, 1 warning`
- Frontend verification:
  - `PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint`
  - Result: TypeScript passed.
  - `PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run build`
  - Result: Next.js production build passed.
- Static verification:
  - `.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests`
  - `git diff --check`

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_nl_draft_patch_preview_ui_v1.md` | proceed to next design | UI implemented and verified with frontend build plus backend endpoint tests. | completed. |
| `design_builder_benchmark_history_ui_v1.md` | proceed to next design | UI implemented and verified with frontend build plus backend history test. | completed. |
| `design_platform_harness_closure_audit_v1.md` | proceed to archive | Audit artifact created and Platform Harness gaps carried forward. | completed. |

## 7. Review Before Archive

- Completion summary: completed UI vertical slices and closure audit.
- Files changed: frontend Studio page/types/i18n/styles; workingon/design docs; experiment status ledger.
- Verification: focused backend tests passed; frontend lint/build passed; compileall passed; diff check passed.
- Remaining risk: no browser screenshot QA yet; no new formal experiments in this version.
- All next-stage tasks dispositioned: yes.
- All accepted tasks expanded into designs: yes.
- Every accepted design completed or explicitly blocked/deferred: yes.
- Engineering closure level claimed: product capability for UI slices; docs/process audit for Platform Harness closure.
- Engineering closure actually achieved: product UI vertical slice for NL preview and benchmark history; docs/process audit for Platform Harness closure.
- Partial slices carried forward: worker lease, secret reference injection, stdio sandbox/container egress, formal experiment suite.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed.
- Experiment deliverables, if any: no new formal experiment in this stage.
- Awaiting user review before archive: no; Automatic Evolution Mode is active.

## 8. Archive Conditions

- All three accepted designs implemented or explicitly blocked.
- Focused frontend/backend checks pass.
- Experiment status latest stage updated.
- Stage report created.
- Current designs archived to `docs/historical-designs/`.
- Workingon files archived to `docs/workingon-archives/v0.2.19/`.

## 9. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.19`.
- Archive automatically after verification: yes.
- Next version selection source: `docs/stage-reports/v0.2.19_*`.
- Continue after archive: yes, unless blocked by missing services, material cost, or user interruption.
