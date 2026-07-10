from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.models import MCPServerSpec, NetworkPolicy
from agent_platform.platform_harness import PlatformHarness, PlatformHarnessViolation
from agent_platform.storage import Storage


def harness(tmp_path: Path, *, platform_policy: str = "allowlist") -> PlatformHarness:
    return PlatformHarness(
        storage=Storage(tmp_path / "data"),
        network_egress_policy=platform_policy,
        network_egress_allowlist=["example.test"],
    )


def test_v02_106_sandboxed_stdio_allowlist_allows_declared_covered_hosts(tmp_path: Path) -> None:
    local_harness = harness(tmp_path)

    decision = local_harness.explain_stdio_mcp_policy(
        surface="test",
        server_name="local",
        agent_network_policy=NetworkPolicy.allowlist,
        sandbox_network_policy=NetworkPolicy.allowlist,
        declared_egress_hosts=["api.example.test"],
        agent_network_allowlist=["example.test"],
    )

    assert decision["allowed"] is True
    assert decision["mode"] == "sandboxed_allowlist"
    assert decision["declared_egress_hosts"] == ["api.example.test"]


def test_v02_106_sandboxed_stdio_allowlist_blocks_missing_declared_hosts(tmp_path: Path) -> None:
    local_harness = harness(tmp_path)

    with pytest.raises(PlatformHarnessViolation, match="requires declared egress_hosts"):
        local_harness.enforce_stdio_mcp_policy(
            surface="test",
            server_name="local",
            agent_network_policy=NetworkPolicy.allowlist,
            sandbox_network_policy=NetworkPolicy.allowlist,
            declared_egress_hosts=[],
            agent_network_allowlist=["example.test"],
        )


def test_v02_106_sandboxed_stdio_allowlist_blocks_platform_unlisted_host(tmp_path: Path) -> None:
    local_harness = harness(tmp_path)

    with pytest.raises(PlatformHarnessViolation, match="not in the platform allowlist"):
        local_harness.enforce_stdio_mcp_policy(
            surface="test",
            server_name="local",
            agent_network_policy=NetworkPolicy.allowlist,
            sandbox_network_policy=NetworkPolicy.allowlist,
            declared_egress_hosts=["api.other.test"],
            agent_network_allowlist=["other.test"],
        )


def test_v02_106_mcp_server_spec_carries_declared_egress_hosts() -> None:
    server = MCPServerSpec(
        name="local",
        transport="stdio",
        command="python",
        egress_hosts=["api.example.test"],
    )

    assert server.egress_hosts == ["api.example.test"]


def test_v02_106_evidence_generator_reports_completed_contract() -> None:
    import importlib.util
    import sys

    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "v02_106_e08_stdio_container_egress_allowlist_contract.py"
    )
    spec = importlib.util.spec_from_file_location("v02_106_evidence_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    result = module.verify_contract()

    assert result["status"] == "completed"
    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert all(result["checks"].values())
