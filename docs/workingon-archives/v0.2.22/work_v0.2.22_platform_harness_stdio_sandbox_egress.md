# work_v0.2.22_platform_harness_stdio_sandbox_egress

## 1. Goal

Implement the next Platform Harness hard-boundary slice from `v0.2.21`: stdio MCP egress guard.

This version closes a concrete bypass: stdio MCP servers do not declare hostnames, so HTTP allowlist/none policy cannot evaluate their outbound network behavior. Until a sandboxed interactive stdio MCP runner exists, restricted policies must block stdio MCP rather than allow an uncontrolled host process.

## 2. Full Task Set Disposition

Source stage report: `docs/stage-reports/v0.2.21_platform_harness_secret_references.md`

| Next-stage task | Disposition | Current-version design(s) | Reason |
| --- | --- | --- | --- |
| Stdio MCP sandbox/container egress | accepted | `docs/current-design/design_platform_harness_stdio_mcp_egress_guard_v1.md`; `docs/current-design/design_platform_harness_stdio_mcp_tests_v1.md` | Close the immediate policy bypass by blocking stdio MCP under restricted platform/agent policies. |
| KMS/envelope encryption or external secret manager integration | deferred | none | Separate secret hardening stage. |
| External worker runner / durable execution queue | deferred | none | Separate worker loop stage. |
| Formal experiment tranche E01/E02/E04/E05/E08 | deferred | none | Requires paid/live experiment plans and DOCX reports. |
| Browser visual QA | deferred | none | Separate UI smoke stage. |
| Platform Harness policy controls UI/API | deferred | none | Should follow policy model stabilization. |

All next-stage tasks listed: yes.

## 3. Plans

| Plan | Current design | Status | Acceptance |
| --- | --- | --- | --- |
| Stdio MCP egress guard | `docs/current-design/design_platform_harness_stdio_mcp_egress_guard_v1.md` | completed | Restricted platform/agent policies block stdio MCP because hostname allowlist cannot apply. |
| Tests and evidence | `docs/current-design/design_platform_harness_stdio_mcp_tests_v1.md` | completed | Focused tests prove both block and full-policy allow behavior. |

## 4. Acceptance Criteria

- Platform Harness exposes an explicit stdio MCP egress policy guard.
- AgentRuntime blocks stdio MCP when agent network policy is `none` or `allowlist`.
- AgentRuntime blocks stdio MCP when platform network egress policy is `none` or `allowlist`.
- Full platform + full agent policy still allows stdio MCP to proceed to the MCP client.
- HTTP MCP behavior remains governed by hostname egress policy.
- Focused tests and full backend regression pass.

## 5. Evidence

Implementation files:

- `platform/backend/src/agent_platform/platform_harness.py`
- `platform/backend/src/agent_platform/runtime.py`
- `platform/backend/src/agent_platform/workflow_runtime.py`
- `tests/test_runtime.py`

Focused stdio MCP guard tests:

```bash
.venv/bin/python -m pytest tests/test_runtime.py::test_runtime_blocks_stdio_mcp_when_agent_network_is_restricted tests/test_runtime.py::test_runtime_blocks_stdio_mcp_when_platform_network_is_restricted tests/test_runtime.py::test_runtime_allows_stdio_mcp_guard_with_full_network_policies -q
```

Result:

- `3 passed`

Full backend regression:

```bash
.venv/bin/python -m pytest -q
```

Result:

- `73 passed, 1 warning`

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
| `design_platform_harness_stdio_mcp_egress_guard_v1.md` | proceed to next design | Platform Harness guard and runtime integration implemented. | completed. |
| `design_platform_harness_stdio_mcp_tests_v1.md` | proceed to archive | Focused tests and full backend regression passed. | completed. |

## 7. Review Before Archive

- Completion summary: completed a hard guard against stdio MCP egress bypass under restricted network policies.
- Engineering closure level claimed: platform boundary slice.
- Engineering closure actually achieved: restricted platform/agent policies block stdio MCP before process start; full/full policy remains allowed.
- Remaining risk: no interactive sandboxed/container stdio MCP runner exists yet.
- Deferred tasks preserved: KMS/envelope encryption, external worker runner, formal experiments, browser visual QA, policy controls UI/API.
- Active current-design will be cleared after archive: yes.
- Active workingon will be cleared after archive: yes.
- Minor version target closure: completed as claimed.

## 8. Automatic Evolution

- Automatic Evolution Mode active: yes.
- Current version: `v0.2.22`.
- Archive automatically after verification: yes.
- Next version selection source after archive: only the v0.2.22 stage report.
