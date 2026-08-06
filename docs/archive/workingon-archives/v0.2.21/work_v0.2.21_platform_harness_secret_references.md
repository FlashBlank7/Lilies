# work_v0.2.21_platform_harness_secret_references

## 1. Goal

Implement the next Platform Harness hard-boundary slice from `v0.2.20`: secret reference injection.

This version must provide a safe path for legitimate secret use without weakening the existing secret-field blocking policy:

- operators can store per-owner secrets through Platform Harness API;
- API responses list only metadata and secret references, never secret values;
- workflow configs can use secret reference objects instead of inline secret values;
- runtime injects secret values only immediately before tool/HTTP execution;
- secret-looking plaintext fields remain blocked;
- tests prove reference injection works and redaction is preserved.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.20_platform_harness_worker_lease.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Secret reference injection | accepted | `docs/current-design/design_platform_harness_secret_store_api_v1.md`; `docs/current-design/design_platform_harness_secret_runtime_injection_v1.md`; `docs/current-design/design_platform_harness_secret_policy_tests_v1.md` | This is the next dedicated Platform Harness gap named by v0.2.20 and can be closed as a backend/runtime/API slice. |
| Stdio MCP sandbox/container egress | deferred | none | Requires process/container policy design after secret references. |
| External worker runner / durable execution queue | deferred | none | Consumes the v0.2.20 lease primitive but is separate from secret injection. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment plans and DOCX reports. |
| Browser visual QA | deferred | none | Separate UI smoke stage. |
| Platform Harness policy controls UI/API | deferred | none | Should follow policy model stabilization. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Secret store and API | `docs/current-design/design_platform_harness_secret_store_api_v1.md` | completed | Store/list/delete secrets without returning values. |
| Runtime injection | `docs/current-design/design_platform_harness_secret_runtime_injection_v1.md` | completed | HTTP/tool payloads can resolve secret references at execution boundary. |
| Policy and tests | `docs/current-design/design_platform_harness_secret_policy_tests_v1.md` | completed | Plaintext secrets stay blocked; references inject and remain redacted in events/API. |

## 4. Acceptance Criteria

- Secret API can create/list/delete per-owner secrets.
- API responses never include raw secret values.
- `{"$secret": "name"}` and `{"$secret": "owner/name", "prefix": "Bearer "}` resolve at runtime.
- Existing inline secret fields are still blocked.
- Missing secret references fail clearly.
- Events and public task/API outputs remain redacted.
- Focused tests and full backend regression pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/storage.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `platform/backend/src/agent_platform/runtime.py`
- `tests/test_workflow.py`

Focused secret reference tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_secret_store_api_redacts_values tests/test_workflow.py::test_platform_harness_secret_reference_injects_http_headers tests/test_workflow.py::test_platform_harness_missing_secret_reference_fails tests/test_workflow.py::test_platform_harness_secret_policy_blocks_http_secret_headers -q
```

Result:

- `4 passed, 1 warning`

Regression after fixing legacy ToolConfig call site:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_tool_egress_policy_blocks_websearch_tool tests/test_workflow.py::test_incremental_workflow_test_publish_restore -q
```

Result:

- `2 passed, 1 warning`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `70 passed, 1 warning`

Static checks:

```bash
.venv/bin/python -m compileall -q platform/backend/src/agent_platform tests
git diff --check
```

Result:

- both passed.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_platform_harness_secret_store_api_v1.md` | proceed to next design | Secret table, store/list/delete API, and redacted public metadata implemented and tested. | completed. |
| `design_platform_harness_secret_runtime_injection_v1.md` | proceed to next design | Workflow HTTP/tool and AgentRuntime tool execution now enforce policy before injection and inject at execution boundary. | completed. |
| `design_platform_harness_secret_policy_tests_v1.md` | proceed to archive | Focused tests and full backend regression passed; missing reference and plaintext blocking are covered. | completed. |

## 7. Review Before Archive

- Completion summary: completed a backend/runtime/API Platform Harness secret reference slice.
- Engineering closure level claimed: platform boundary slice.
- Engineering closure actually achieved: local secret store, redacted API, runtime reference injection, missing-reference failure, inline-secret blocking, regression coverage.
- Remaining risk: local SQLite secret values are not KMS/envelope encrypted; production-grade KMS integration is a later Platform Harness hardening task.
- Deferred tasks preserved: stdio sandbox egress, external worker runner, formal experiments, browser visual QA, policy controls UI/API.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.21`.
- Archive automatically after verification: yes.
- Next version selection source after archive: only the v0.2.21 stage report.
