# work_v0.2.42_builder_build_level_watchdog

## 1. Goal

Add a Builder build-level watchdog/progress boundary.

v0.2.40 made individual provider stream timeouts observable. v0.2.41 showed that whole Builder runs can still consume substantial time/calls. This stage adds an optional whole-build elapsed-time boundary that is persisted, visible in Platform Harness metadata/events, and covered by deterministic tests.

## 2. Scope

Included:

- Add optional `max_elapsed_seconds` to Builder build requests.
- Persist the value with build records.
- Include the value in Platform Harness task metadata.
- Emit deadline configured/exceeded build events.
- Convert build-level timeout into structured `build_timeout` failure metadata.
- Add deterministic API/Builder test.

Excluded:

- UI controls for `max_elapsed_seconds`.
- Paid/live provider timeout testing.
- Build retry policy redesign.
- Distributed worker deadlines.
- Broad E05 rerun after this boundary.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Add Builder build-level watchdog | `docs/current-design/design_v0.2.42_builder_build_level_watchdog_v1.md` | completed | Build requests can set a whole-build deadline; timeout becomes needs_attention with Harness metadata and stream events. |

## 4. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.41_e05_success_condition_after_timeout_boundary.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Add a Builder build-level watchdog/progress boundary. | accepted | `design_v0.2.42_builder_build_level_watchdog_v1.md` | Recommended handoff and direct reliability prerequisite before broader paid experiments. |
| Continue E05 with additional task families after the build-level boundary exists. | deferred | none | Should run after this watchdog boundary lands. |
| Run E08 workflow-internal gate vs sidecar monitor/passmode comparison. | deferred | none | Separate Harness experiment stage. |
| Continue deferred Platform Harness product tasks with explicit closure level. | deferred | none | Separate product/platform stage. |
| Run actual E02 human-panel review if a human reviewer pool becomes available. | deferred | none | No human reviewer pool in this execution context. |
| Broaden E04 failure classes. | deferred | none | Separate repair-policy experiment. |
| Add more complex plan-first cases. | deferred | none | Optional product-strategy evidence. |

Every next-stage task is listed and dispositioned.

## 5. Evidence

Implementation evidence:

- `docs/workingon/implementation_v0.2.42_builder_build_level_watchdog.md`

Verification:

- `.venv/bin/python -m pytest tests/test_workflow.py::test_builder_build_level_watchdog_records_harness_metadata -q`
  - Result: `1 passed, 1 warning`
- `.venv/bin/python -m pytest tests/test_workflow.py::test_builder_records_provider_timeout_in_harness_metadata tests/test_workflow.py::test_builder_build_level_watchdog_records_harness_metadata -q`
  - Result: `2 passed, 1 warning`
- `.venv/bin/python -m pytest -q`
  - Result: `103 passed, 1 warning`
- `.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts`
  - Result: successful compileall

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_v0.2.42_builder_build_level_watchdog_v1.md` | completed | Implementation and regression tests completed. | Archive to historical design. |

## 7. Review Before Archive

- Completion summary: Builder now has optional build-level elapsed-time watchdog with Harness metadata and events.
- Files changed: `workflow_models.py`, `workflow_storage.py`, `api.py`, `builder.py`, `tests/test_workflow.py`.
- Verification: focused watchdog test, provider/build timeout distinction tests, full pytest, compileall.
- Remaining risk: no UI surface yet; post-agent-loop validation paths are not separately wrapped; no paid/live rerun needed for deterministic boundary slice.
- All next-stage tasks dispositioned: yes
- All accepted tasks expanded into designs: yes
- Every accepted design completed or explicitly blocked/deferred: yes
- Engineering closure level claimed: backend slice + platform boundary slice
- Engineering closure actually achieved: backend slice + platform boundary slice
- Active current-design will be cleared after archive: yes
- Active workingon will be cleared after archive: yes
- Minor version target closure: achieved for accepted v0.2.42 task set
- Awaiting user review before archive: no, Automatic Evolution Mode archives automatically

## 8. Archive Conditions

- Deterministic build-level watchdog test passes.
- Full backend regression passes.
- Stage report records compatibility boundary and any deferred UI/product work.
- Historical design written with `v0.2.42_` filename.
- Active `docs/current-design/` and `docs/workingon/` are cleared to README only.
- Commit created with explicit staged path list.

## 9. Automatic Evolution

- Automatic Evolution Mode active: yes
- Current version: `v0.2.42`
- Archive automatically after verification: yes
- Next version selection source: current stage report to be created after completion
- Continue after archive: yes
