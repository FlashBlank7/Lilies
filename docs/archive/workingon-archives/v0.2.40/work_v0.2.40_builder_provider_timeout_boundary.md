# work_v0.2.40_builder_provider_timeout_boundary

## 1. Goal

Fix the Builder/provider timeout task boundary surfaced by v0.2.39 paid/live validation.

The target is not to make every model timeout disappear. The target is to make model/provider stream timeouts deterministic, observable, and recorded in the Platform Harness task boundary instead of appearing as a long ambiguous `building` period with thin evidence.

## 2. Scope

Included:

- Add model stream timeout/error events in `AgentRuntime._collect_stream()`.
- Use the configured `settings.deepseek_timeout_seconds` as the default model stream hard boundary when no narrower timeout is passed.
- Preserve existing provider-level `ProviderError` behavior.
- Update Builder failure handling so ProviderError details are recorded in Platform Harness task metadata.
- Add deterministic tests for Builder timeout/error boundary behavior.

Excluded:

- Changing paid provider APIs or the DeepSeek HTTP client contract.
- Retrying strategy redesign.
- Full E05 rerun after timeout handling.
- UI changes for timeout display.
- E08 sidecar/passmode experiment.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Add model stream timeout/error events | `docs/current-design/design_v0.2.40_model_stream_timeout_boundary_v1.md` | completed | A slow provider stream produces deterministic timeout events and a retryable ProviderError. |
| Add Builder Harness timeout metadata and tests | `docs/current-design/design_v0.2.40_builder_timeout_harness_metadata_v1.md` | completed | Builder timeout/ProviderError failures finish the Platform Harness task with classified metadata and regression tests. |

## 4. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.39_template_reuse_expandability_contract.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Fix Builder/provider timeout task boundary. | accepted | `design_v0.2.40_model_stream_timeout_boundary_v1.md`; `design_v0.2.40_builder_timeout_harness_metadata_v1.md` | This is the recommended handoff and a concrete industrial reliability issue from v0.2.39. |
| Continue E05 success-condition validation after timeout handling. | deferred | none | Must wait until timeout boundary is deterministic; should be the next E05 stage after v0.2.40. |
| Run E08 workflow-internal gate vs sidecar monitor/passmode comparison. | deferred | none | Separate Harness experiment stage. |
| Continue deferred Platform Harness product tasks with explicit closure level. | deferred | none | Separate product/platform boundary stage. |
| Run actual E02 human-panel review if a human reviewer pool becomes available. | deferred | none | No human reviewer pool in this execution context. |
| Broaden E04 failure classes. | deferred | none | Separate repair-policy experiment. |
| Add more complex plan-first cases. | deferred | none | Optional product-strategy evidence. |

Every next-stage task is listed and dispositioned.

## 5. Evidence

Implementation evidence:

- `docs/workingon/implementation_v0.2.40_builder_provider_timeout_boundary.md`

Verification:

- `.venv/bin/python -m pytest tests/test_runtime.py::test_collect_stream_timeout_emits_retryable_provider_error tests/test_workflow.py::test_builder_records_provider_timeout_in_harness_metadata -q`
  - Result: `2 passed, 1 warning`
- `.venv/bin/python -m pytest -q`
  - Result: `101 passed, 1 warning`
- `.venv/bin/python -m compileall platform/backend/src/agent_platform tests scripts`
  - Result: successful compileall

Paid/live validation:

- Not rerun in this stage.
- v0.2.39 already supplied the paid/live DeepSeek timeout evidence that motivated this deterministic boundary fix.
- Forcing another real timeout would be nondeterministic and would not increase confidence in this code path.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_v0.2.40_model_stream_timeout_boundary_v1.md` | completed | Runtime timeout boundary implemented and tested. | Archive to historical design. |
| `design_v0.2.40_builder_timeout_harness_metadata_v1.md` | completed | Builder Harness failure metadata implemented and tested. | Archive to historical design. |

## 7. Review Before Archive

- Completion summary: Builder/provider timeout failures now have deterministic runtime timeout events and Platform Harness failure metadata.
- Files changed: `runtime.py`, `builder.py`, `tests/test_runtime.py`, `tests/test_workflow.py`.
- Verification: focused tests, full pytest, compileall all passed.
- Remaining risk: UI does not yet expose the structured failure metadata; E05 paid/live success-condition validation remains a later stage.
- All next-stage tasks dispositioned: yes
- All accepted tasks expanded into designs: yes
- Every accepted design completed or explicitly blocked/deferred: yes
- Engineering closure level claimed: backend slice + platform boundary slice
- Engineering closure actually achieved: backend slice + platform boundary slice
- Partial slices carried forward: E05 post-timeout rerun and UI surfacing
- Active current-design will be cleared after archive: yes
- Active workingon will be cleared after archive: yes
- Minor version target closure: achieved for accepted v0.2.40 task set
- Experiment deliverables, if any: none unless paid/live validation is run
- Awaiting user review before archive: no, Automatic Evolution Mode archives automatically

## 8. Archive Conditions

- Deterministic timeout tests pass.
- Full backend regression passes.
- Stage report explicitly records whether paid/live validation was required or deferred.
- Historical designs are written with `v0.2.40_` filenames.
- Active `docs/current-design/` and `docs/workingon/` are cleared to README only.
- Commit created with explicit staged path list.

## 9. Automatic Evolution

- Automatic Evolution Mode active: yes
- Current version: `v0.2.40`
- Archive automatically after verification: yes
- Next version selection source: current stage report to be created after completion
- Continue after archive: yes
