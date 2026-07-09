# work_v0.2.28_worker_heartbeat_and_renewal

## 1. Goal

Implement the next automatic-evolution slice from `v0.2.27`: add lease renewal / heartbeat evidence for long-running Platform Harness worker handlers.

This version does not claim full distributed queue semantics. It closes the immediate risk that a real worker handler can outlive its lease and fail at finish time.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.27_worker_runner_cli_and_handler.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Add lease renewal loop and worker heartbeat for long-running handlers | accepted | `docs/current-design/design_worker_runner_lease_renewal_v1.md`; `docs/current-design/design_worker_runner_renewal_tests_v1.md` | Direct continuation from v0.2.27 worker handler. |
| More real worker handlers for build/test/workflow-run tasks | deferred | none | Separate handler catalog stage. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment designs and DOCX reports. |
| Browser visual QA | deferred | none | Separate UI QA stage. |
| Editable Platform Harness policy controls | deferred | none | Requires product decision. |
| Allowlist-grade stdio MCP sandbox firewalling | deferred | none | Requires deeper sandbox/firewall design. |
| KMS/external secret manager, key rotation, legacy migration | deferred | none | Separate secret-hardening stage. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Worker lease renewal loop | `docs/current-design/design_worker_runner_lease_renewal_v1.md` | completed | Runner renews lease while handler is running and records renewal count. |
| Renewal tests and archive | `docs/current-design/design_worker_runner_renewal_tests_v1.md` | completed | Short-lease long-handler test proves renewal and successful finish. |

## 4. Acceptance Criteria

- `PlatformHarnessWorkerRunner` renews task lease while a handler is running.
- Renewal interval is configurable and defaults to half the lease duration.
- Task metadata records renewal count.
- Focused short-lease test proves lease version increases beyond claim.
- Full backend regression and compileall pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/worker_runner.py`
- `tests/test_workflow.py`

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_worker_runner_completes_queued_task tests/test_workflow.py::test_platform_harness_worker_runner_marks_handler_failure tests/test_workflow.py::test_platform_harness_worker_runner_renews_lease_for_long_handler tests/test_workflow.py::test_platform_worker_scheduler_manual_trigger_handler_runs_workflow -q
```

Result:

- `4 passed, 1 warning`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `85 passed, 1 warning`

Static compile:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
```

Result:

- passed.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_worker_runner_lease_renewal_v1.md` | proceed to next design | Renewal loop implemented and metadata includes renewal count. | completed. |
| `design_worker_runner_renewal_tests_v1.md` | proceed to archive | Focused renewal test, full backend regression, and compileall passed. | completed. |

## 7. Review Before Archive

- Completion summary: completed worker lease renewal loop and heartbeat evidence.
- Engineering closure level claimed: platform boundary slice.
- Engineering closure actually achieved: configurable renewal interval, background renewal while handler runs, renewal count metadata, short-lease regression.
- Remaining risk: no distributed heartbeat registry, no process supervisor, no renewal failure escalation beyond task metadata/final status.
- Deferred tasks preserved: more handlers, formal experiments, browser visual QA, editable policy controls, stdio allowlist firewalling, KMS/key rotation/migration.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.28`.
- Archive automatically after verification: yes.
