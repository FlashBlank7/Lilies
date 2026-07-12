#!/usr/bin/env python3
"""Generate v0.2.106 E08 stdio/container egress allowlist evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract"


def verify_contract() -> dict[str, Any]:
    import sys

    backend_src = ROOT / "platform" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))

    from agent_platform.models import NetworkPolicy  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.platform_harness import (  # pylint: disable=import-error,import-outside-toplevel
        PlatformHarness,
        PlatformHarnessViolation,
    )
    from agent_platform.storage import Storage  # pylint: disable=import-error,import-outside-toplevel

    harness = PlatformHarness(
        storage=Storage(ROOT / ".tmp" / "v02_106_e08_stdio_contract"),
        network_egress_policy="allowlist",
        network_egress_allowlist=["example.test"],
    )
    allowed = harness.explain_stdio_mcp_policy(
        surface="evidence",
        server_name="declared-local",
        agent_network_policy=NetworkPolicy.allowlist,
        sandbox_network_policy=NetworkPolicy.allowlist,
        declared_egress_hosts=["api.example.test"],
        agent_network_allowlist=["example.test"],
    )
    missing_host = harness.explain_stdio_mcp_policy(
        surface="evidence",
        server_name="missing-hosts",
        agent_network_policy=NetworkPolicy.allowlist,
        sandbox_network_policy=NetworkPolicy.allowlist,
        declared_egress_hosts=[],
        agent_network_allowlist=["example.test"],
    )
    unlisted_host = harness.explain_stdio_mcp_policy(
        surface="evidence",
        server_name="unlisted-host",
        agent_network_policy=NetworkPolicy.allowlist,
        sandbox_network_policy=NetworkPolicy.allowlist,
        declared_egress_hosts=["api.other.test"],
        agent_network_allowlist=["other.test"],
    )
    unsandboxed = harness.explain_stdio_mcp_policy(
        surface="evidence",
        server_name="unsandboxed",
        agent_network_policy=NetworkPolicy.allowlist,
        sandbox_network_policy=None,
        declared_egress_hosts=["api.example.test"],
        agent_network_allowlist=["example.test"],
    )
    try:
        harness.enforce_stdio_mcp_policy(
            surface="evidence",
            server_name="unlisted-host",
            agent_network_policy=NetworkPolicy.allowlist,
            sandbox_network_policy=NetworkPolicy.allowlist,
            declared_egress_hosts=["api.other.test"],
            agent_network_allowlist=["other.test"],
        )
        blocked_before_action = False
    except PlatformHarnessViolation:
        blocked_before_action = True

    checks = {
        "covered_sandboxed_stdio_allowlist_allowed": allowed["allowed"] is True
        and allowed["mode"] == "sandboxed_allowlist",
        "missing_declared_hosts_blocked": missing_host["allowed"] is False
        and "declared egress_hosts" in missing_host["reason"],
        "platform_unlisted_host_blocked": unlisted_host["allowed"] is False
        and "platform allowlist" in unlisted_host["reason"],
        "unsandboxed_allowlist_blocked": unsandboxed["allowed"] is False
        and "requires sandboxed execution" in unsandboxed["reason"],
        "blocked_before_external_action": blocked_before_action,
        "policy_controls_allowlist_supported": harness.policy_controls()["stdio_mcp"]["allowlist_supported"] is True,
    }
    return {
        "version": "v0.2.106",
        "evidence_id": "e08_stdio_container_egress_allowlist_contract",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.105_e08_broader_sidecar_scope_decomposition.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "decisions": {
            "allowed": allowed,
            "missing_host": missing_host,
            "unlisted_host": unlisted_host,
            "unsandboxed": unsandboxed,
        },
        "implementation_paths": [
            "platform/backend/src/agent_platform/models.py",
            "platform/backend/src/agent_platform/platform_harness.py",
            "platform/backend/src/agent_platform/runtime.py",
            "platform/backend/src/agent_platform/workflow_runtime.py",
            "tests/test_v02_106_e08_stdio_container_egress_allowlist_contract.py",
            "tests/test_runtime.py",
            "tests/test_workflow.py",
        ],
        "existing_evidence_preserved": [
            "docs/stage-report-archives/v0.2.x/v0.2.22_platform_harness_stdio_sandbox_egress.md",
            "docs/stage-report-archives/v0.2.x/v0.2.24_platform_harness_stdio_policy_controls.md",
        ],
        "invariants": {
            "e08_full_sidecar_completion_claimed": False,
            "current_tranche_not_duplicated": True,
            "workingon_is_not_task_source": True,
        },
        "next_boundary": (
            "This closes the stdio/container egress allowlist contract slice only; KMS/rotation, "
            "complete handler catalog, distributed heartbeat registry, and other sidecar slices remain open."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.106 E08 stdio/container egress allowlist contract",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Next boundary: {result['next_boundary']}",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, value in result["checks"].items():
        lines.append(f"| `{name}` | `{value}` |")
    lines.extend(["", "## Implementation Paths", ""])
    for path in result["implementation_paths"]:
        lines.append(f"- `{path}`")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = verify_contract()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
