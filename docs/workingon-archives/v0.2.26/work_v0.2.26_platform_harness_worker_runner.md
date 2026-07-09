# work_v0.2.26_platform_harness_worker_runner

## 1. Goal

Implement the next automatic-evolution slice from `v0.2.25`: add a narrow external worker runner primitive that consumes Platform Harness worker leases.

This version does not claim a full distributed execution queue. It must prove that queued Platform Harness tasks can be claimed by a worker, dispatched through a registered handler, and finished with durable task status and metadata.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.25_platform_harness_secret_envelope.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Add external worker runner / durable execution queue | accepted: worker runner primitive | `docs/current-design/design_platform_harness_worker_runner_core_v1.md`; `docs/current-design/design_platform_harness_worker_runner_tests_v1.md` | Consumes v0.2.20 lease primitive in a real runner loop without claiming full queue semantics. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment designs and DOCX reports. |
| Browser visual QA | deferred | none | Separate UI QA stage. |
| Editable Platform Harness policy controls | deferred | none | Requires product decision on runtime policy editing. |
| Allowlist-grade stdio MCP sandbox firewalling | deferred | none | Requires deeper sandbox/firewall design. |
| KMS/external secret manager, key rotation, legacy migration | deferred | none | Separate secret-hardening stage after local envelope slice. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Worker runner core | `docs/current-design/design_platform_harness_worker_runner_core_v1.md` | completed | Runner claims queued tasks, dispatches registered handlers, and finishes tasks. |
| Worker runner tests and archive | `docs/current-design/design_platform_harness_worker_runner_tests_v1.md` | completed | Success, failure, no-handler skip, full backend regression, and archive evidence pass. |

## 4. Acceptance Criteria

- A reusable backend worker runner class exists.
- Runner claims queued tasks with worker leases and lease metadata.
- Registered handlers can complete tasks with result metadata.
- Handler failures mark tasks failed with error.
- Tasks without registered handlers are skipped without mutating task state.
- Focused tests and full backend regression pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/worker_runner.py`
- `tests/test_workflow.py`

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_worker_runner_completes_queued_task tests/test_workflow.py::test_platform_harness_worker_runner_marks_handler_failure tests/test_workflow.py::test_platform_harness_worker_runner_skips_unsupported_task -q
```

Result:

- `3 passed, 1 warning`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `82 passed, 1 warning`

Static compile:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
```

Result:

- passed.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_platform_harness_worker_runner_core_v1.md` | proceed to next design | Worker runner class implemented. | completed. |
| `design_platform_harness_worker_runner_tests_v1.md` | proceed to archive | Focused runner tests, full backend regression, and compileall passed. | completed. |

## 7. Review Before Archive

- Completion summary: completed worker runner primitive for queued Platform Harness tasks.
- Engineering closure level claimed: platform boundary slice.
- Engineering closure actually achieved: reusable runner class, lease claim, handler dispatch, success/failure finish, unsupported skip, and regression tests.
- Remaining risk: no CLI process, no handler catalog for real workflow/build tasks, no distributed queue backend, no long-running renew loop.
- Deferred tasks preserved: formal experiments, browser visual QA, editable policy controls, allowlist-grade stdio firewalling, KMS/key rotation/migration.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.26`.
- Archive automatically after verification: yes.
