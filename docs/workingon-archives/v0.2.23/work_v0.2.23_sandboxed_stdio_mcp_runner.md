# work_v0.2.23_sandboxed_stdio_mcp_runner

## 1. Goal

Implement the next stdio MCP hardening slice from `v0.2.22`: a sandboxed stdio MCP runner path.

This version narrows the target to a concrete closure: stdio MCP can run through the existing sandbox session when sandbox network policy is `none`, so restricted no-network use can be controlled by container network isolation. Allowlist-grade stdio egress remains deferred because the current sandbox only passes `AGENT_NETWORK_ALLOWLIST` and does not enforce per-host firewalling.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-report-archives/v0.2.x/v0.2.22_platform_harness_stdio_sandbox_egress.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Interactive sandboxed stdio MCP runner | accepted | `docs/current-design/design_sandboxed_stdio_mcp_runner_v1.md`; `docs/current-design/design_sandboxed_stdio_mcp_policy_tests_v1.md` | Implements the smallest runner path that consumes v0.2.22 guard without claiming allowlist firewalling. |
| KMS/envelope encryption or external secret manager integration | deferred | none | Separate secret hardening stage. |
| External worker runner / durable execution queue | deferred | none | Separate worker loop stage. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment plans and DOCX reports. |
| Browser visual QA | deferred | none | Separate UI smoke stage. |
| Platform Harness policy controls UI/API | deferred | none | Separate policy UI/API stage. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Sandboxed stdio MCP runner | `docs/current-design/design_sandboxed_stdio_mcp_runner_v1.md` | completed | MCP client can run stdio JSON-RPC through sandbox session. |
| Policy tests | `docs/current-design/design_sandboxed_stdio_mcp_policy_tests_v1.md` | completed | Guard allows sandboxed `none` policy and keeps unsafe paths blocked. |

## 4. Acceptance Criteria

- `MCPClient` supports a sandboxed stdio execution path.
- `MCPTool` passes the current sandbox session to stdio MCP calls.
- Runtime policy guard allows stdio MCP when both the execution path is sandboxed and the effective sandbox network policy is `none`.
- Allowlist stdio MCP remains blocked until a hard allowlist mechanism exists.
- Focused tests and full backend regression pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/tools/mcp.py`
- `platform/backend/src/agent_platform/tools/core.py`
- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/runtime.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `tests/test_runtime.py`

Focused sandboxed stdio MCP tests:

```bash
.venv/bin/python -m pytest tests/test_runtime.py::test_runtime_allows_sandboxed_stdio_mcp_with_no_network_policy tests/test_runtime.py::test_runtime_blocks_sandboxed_stdio_mcp_with_allowlist_policy tests/test_runtime.py::test_mcp_client_runs_stdio_bridge_inside_sandbox tests/test_runtime.py::test_runtime_blocks_stdio_mcp_when_agent_network_is_restricted tests/test_runtime.py::test_runtime_blocks_stdio_mcp_when_platform_network_is_restricted -q
```

Result:

- `5 passed`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `76 passed, 1 warning`

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
| `design_sandboxed_stdio_mcp_runner_v1.md` | proceed to next design | MCPClient sandbox bridge and MCPTool sandbox forwarding implemented and tested. | completed. |
| `design_sandboxed_stdio_mcp_policy_tests_v1.md` | proceed to archive | Guard behavior for unsandboxed restricted, sandboxed none, sandboxed allowlist, and full/full policies tested. | completed. |

## 7. Review Before Archive

- Completion summary: completed sandboxed stdio MCP runner path for no-network sandbox usage.
- Engineering closure level claimed: platform boundary slice.
- Engineering closure actually achieved: stdio MCP can run through sandbox bridge; guard permits sandboxed `none` policy and blocks allowlist/unsandboxed restricted paths.
- Remaining risk: allowlist-grade stdio MCP requires real per-host sandbox firewalling; not implemented.
- Deferred tasks preserved: KMS/envelope encryption, external worker runner, formal experiments, browser visual QA, policy controls UI/API.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.23`.
- Archive automatically after verification: yes.
