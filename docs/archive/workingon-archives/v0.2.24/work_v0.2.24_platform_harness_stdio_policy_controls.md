# work_v0.2.24_platform_harness_stdio_policy_controls

## 1. Goal

Implement the next automatic-evolution slice from `v0.2.23`: expose Platform Harness stdio MCP policy controls and blocked reasons to operators.

This version is not a firewall implementation. It closes a product/platform visibility gap: after v0.2.22/v0.2.23, stdio MCP has real enforcement behavior, but the Studio and API do not explain which policy combinations are allowed or blocked.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.23_sandboxed_stdio_mcp_runner.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Add allowlist-grade stdio MCP sandbox firewalling or explicit policy-control path | accepted: policy-control path | `docs/current-design/design_platform_harness_policy_controls_api_v1.md`; `docs/current-design/design_platform_harness_policy_controls_ui_v1.md`; `docs/current-design/design_platform_harness_policy_controls_tests_v1.md` | Exposes current true behavior and keeps stdio allowlist blocked until hard firewalling exists. |
| KMS/envelope encryption or external secret-manager integration | deferred | none | Separate secret-hardening version; current version should not mix secrets and stdio policy controls. |
| External worker runner / durable execution queue | deferred | none | Separate durable worker execution version. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment designs, raw evidence, and DOCX reports. |
| Browser visual QA | deferred, except build-level frontend verification | none | Full browser visual QA remains a separate stage; this version will run lint/build for UI integrity. |
| Platform Harness policy controls UI/API | accepted | Same three designs | This is the user-facing half of the stdio policy-control path. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Backend policy explanation API | `docs/current-design/design_platform_harness_policy_controls_api_v1.md` | completed | API returns network policy, allowlist, secret/lease settings, and stdio MCP scenario decisions. |
| Studio policy controls panel | `docs/current-design/design_platform_harness_policy_controls_ui_v1.md` | completed | Monitor tab shows current policy and stdio allowed/blocked reason cards. |
| Regression tests and archive evidence | `docs/current-design/design_platform_harness_policy_controls_tests_v1.md` | completed | API tests, frontend lint/build, backend pytest, and archive evidence pass. |

## 4. Acceptance Criteria

- Backend exposes `GET /api/v1/platform/harness/policy-controls`.
- Stdio MCP policy explanation is shared by enforcement and API visibility, so UI text does not drift from runtime behavior.
- API shows that sandboxed no-network stdio MCP is supported and allowlist-grade stdio MCP is intentionally blocked.
- Studio monitor tab displays policy controls without requiring a workflow run.
- Tests prove the API reports the blocked allowlist reason.
- Full backend regression and frontend lint/build pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/api.py`
- `platform/frontend/lib/platform.ts`
- `platform/frontend/lib/i18n.ts`
- `platform/frontend/app/applications/[id]/page.tsx`
- `platform/frontend/app/globals.css`
- `tests/test_workflow.py`

Focused tests:

```bash
.venv/bin/python -m pytest tests/test_workflow.py::test_platform_harness_policy_controls_api_reports_stdio_mcp_decisions tests/test_runtime.py::test_runtime_blocks_sandboxed_stdio_mcp_with_allowlist_policy tests/test_runtime.py::test_runtime_allows_sandboxed_stdio_mcp_with_no_network_policy -q
```

Result:

- `3 passed, 1 warning`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `77 passed, 1 warning`

Frontend verification:

```bash
cd platform/frontend && PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint
cd platform/frontend && PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run build
```

Result:

- lint passed.
- build passed.

## 6. Design Execution Decisions

| Design | Decision | Reason | Next action |
| --- | --- | --- | --- |
| `design_platform_harness_policy_controls_api_v1.md` | proceed to next design | Shared policy explanation and API implemented and tested. | completed. |
| `design_platform_harness_policy_controls_ui_v1.md` | proceed to next design | Studio monitor policy panel implemented and TypeScript/build verified. | completed. |
| `design_platform_harness_policy_controls_tests_v1.md` | proceed to archive | Focused, full backend, frontend lint, and frontend build passed. | completed. |

## 7. Review Before Archive

- Completion summary: completed Platform Harness policy controls visibility for stdio MCP enforcement.
- Engineering closure level claimed: product capability slice.
- Engineering closure actually achieved: backend API, shared enforcement explanation, Studio monitor panel, focused tests, full backend regression, frontend lint/build.
- Remaining risk: browser visual QA still deferred; policy controls are read-only and do not implement allowlist-grade stdio firewalling.
- Deferred tasks preserved: KMS/envelope encryption, external worker runner, formal experiments, browser visual QA, and deeper policy editing controls.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.24`.
- Archive automatically after verification: yes.
