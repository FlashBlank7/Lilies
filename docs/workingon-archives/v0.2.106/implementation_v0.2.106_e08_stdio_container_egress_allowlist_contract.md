# v0.2.106 E08 stdio/container egress allowlist contract implementation summary

## Source

- Source stage report: `docs/stage-reports/v0.2.105_e08_broader_sidecar_scope_decomposition.md`
- Source task set: `Implement stdio/container egress allowlist contract`; `Preserve existing stdio/sandbox evidence`; `Preserve full-sidecar boundary`; `Maintain executable verification discipline`

## Implemented

- Added `egress_hosts` to `MCPServerSpec`.
- Added Platform Harness stdio allowlist contract:
  - sandboxed stdio allowlist requires agent and sandbox allowlist policies;
  - declared `egress_hosts` must be non-empty;
  - declared hosts must be covered by agent allowlist;
  - platform allowlist, when active, must also cover declared hosts.
- Wired runtime and workflow runtime enforcement to pass `egress_hosts` and agent allowlist.
- Updated policy-controls stdio decision surface to report allowlist support and contract requirements.
- Added v0.2.106 contract tests and evidence generator.

## Verified Behavior

- Covered sandboxed stdio allowlist is allowed.
- Missing `egress_hosts` is blocked before execution.
- Unlisted declared host is blocked before execution.
- Untrusted unsandboxed allowlist stdio remains blocked.
- Existing full/full and sandboxed no-network paths remain valid.

## Boundary Preservation

- This closes only the stdio/container egress allowlist contract slice.
- Full Platform Harness sidecar completion is not claimed.
- KMS/rotation, complete handler catalog, distributed heartbeat registry, and other E08 slices remain open.

## Verification

- `.venv/bin/python scripts/v02_106_e08_stdio_container_egress_allowlist_contract.py`
- `.venv/bin/python -m pytest tests/test_v02_106_e08_stdio_container_egress_allowlist_contract.py -q`
- `.venv/bin/python -m pytest tests/test_runtime.py::test_runtime_blocks_stdio_mcp_when_agent_network_is_restricted tests/test_runtime.py::test_runtime_blocks_stdio_mcp_when_platform_network_is_restricted tests/test_runtime.py::test_runtime_allows_stdio_mcp_guard_with_full_network_policies tests/test_runtime.py::test_runtime_allows_sandboxed_stdio_mcp_with_no_network_policy tests/test_runtime.py::test_runtime_allows_sandboxed_stdio_mcp_with_allowlist_policy tests/test_runtime.py::test_runtime_blocks_sandboxed_stdio_mcp_with_missing_egress_hosts tests/test_runtime.py::test_runtime_blocks_sandboxed_stdio_mcp_with_unlisted_declared_host tests/test_workflow.py::test_platform_harness_policy_controls_api_reports_stdio_mcp_decisions -q`

## Final Status

Completed for v0.2.106 archive.
