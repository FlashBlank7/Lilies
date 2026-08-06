# work_v0.2.27_worker_runner_cli_and_handler

## 1. Goal

Implement the next automatic-evolution slice from `v0.2.26`: make the worker runner operator-usable and add one real Platform Harness task handler.

This version does not claim full distributed queue completion. It adds a CLI/entrypoint plus a `scheduler_manual_trigger` handler that runs through the existing scheduler and workflow runtime.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.26_platform_harness_worker_runner.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Add daemonized/CLI worker process and one real Platform Harness task handler | accepted | `docs/current-design/design_worker_runner_cli_v1.md`; `docs/current-design/design_worker_runner_scheduler_handler_v1.md`; `docs/current-design/design_worker_runner_cli_handler_tests_v1.md` | Direct continuation from v0.2.26 primitive. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment designs and DOCX reports. |
| Browser visual QA | deferred | none | Separate UI QA stage. |
| Editable Platform Harness policy controls | deferred | none | Requires product decision. |
| Allowlist-grade stdio MCP sandbox firewalling | deferred | none | Requires deeper sandbox/firewall design. |
| KMS/external secret manager, key rotation, legacy migration | deferred | none | Separate secret-hardening stage. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Worker CLI/entrypoint | `docs/current-design/design_worker_runner_cli_v1.md` | completed | CLI can run once or loop using configured services. |
| Scheduler manual trigger handler | `docs/current-design/design_worker_runner_scheduler_handler_v1.md` | completed | Queued `scheduler_manual_trigger` task launches a workflow run through the existing scheduler/runtime. |
| Regression tests and archive | `docs/current-design/design_worker_runner_cli_handler_tests_v1.md` | completed | Handler integration, CLI smoke, full pytest, and archive evidence pass. |

## 4. Acceptance Criteria

- A script entrypoint exists for worker runner operation.
- Built-in handler registry includes `scheduler_manual_trigger`.
- Handler consumes the queued worker task rather than creating an unrelated parent boundary.
- Integration test proves a queued scheduler manual trigger task starts a workflow run and finishes the Platform Harness task.
- Full backend regression and compileall pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/worker_runner.py`
- `platform/backend/src/agent_platform/scheduler.py`
- `pyproject.toml`
- `tests/test_workflow.py`

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_worker_runner_helper_imports tests/test_workflow.py::test_platform_worker_scheduler_manual_trigger_handler_runs_workflow -q
```

Result:

- `2 passed, 1 warning`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `84 passed, 1 warning`

Static compile:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
```

Result:

- passed.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_worker_runner_cli_v1.md` | proceed to next design | `agent-platform-worker` script and helper functions implemented. | completed. |
| `design_worker_runner_scheduler_handler_v1.md` | proceed to next design | Built-in scheduler manual trigger handler implemented and integrated with existing harness task parent boundary. | completed. |
| `design_worker_runner_cli_handler_tests_v1.md` | proceed to archive | Focused integration, full backend regression, and compileall passed. | completed. |

## 7. Review Before Archive

- Completion summary: completed operator entrypoint/helper plus one real scheduler manual trigger handler.
- Engineering closure level claimed: platform boundary slice.
- Engineering closure actually achieved: CLI script registration, one-shot helper, built-in handler registry, scheduler parent-task support, real schedule workflow integration test.
- Remaining risk: no long-running renewal loop, no production process manager, no handler catalog for build/test/workflow tasks beyond scheduler manual trigger.
- Deferred tasks preserved: formal experiments, browser visual QA, editable policy controls, stdio allowlist firewalling, KMS/key rotation/migration.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.27`.
- Archive automatically after verification: yes.
