#!/usr/bin/env python3
"""Generate v0.2.86 evidence for frontend verification environment repair."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate

from frontend_verification_runner import ROOT, run_frontend_verification


OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "verification_v0.2.86_frontend_environment_repair"


def build_repair_evidence() -> dict[str, Any]:
    verification = run_frontend_verification()
    safety = complexity_router_default_safety_gate()
    return {
        "version": "v0.2.86",
        "repair_id": "frontend_verification_environment_repair",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.85_complexity_router_post_operator_opt_in_decision.md",
        "verification": verification,
        "status": verification["status"],
        "pass_fail": {
            "passed": verification["passed"],
            "reason": verification["reason"],
        },
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    probe = result["verification"]["probe"]
    lines = [
        "# v0.2.86 frontend verification environment repair",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Reason: {result['pass_fail']['reason']}",
        f"- Selected Node bin: `{probe['selected_node_bin']}`",
        f"- Node available: `{probe['node_available']}`",
        f"- npm available: `{probe['npm_available']}`",
        f"- package.json present: `{probe['package_json_present']}`",
        f"- node_modules present: `{probe['node_modules_present']}`",
        f"- Default enabled: `{result['default_enabled']}`",
        f"- Allowed to enable default: `{result['allowed_to_enable_default']}`",
        "",
        "| Check | Return code | Passed |",
        "| --- | ---: | --- |",
    ]
    for check in result["verification"]["checks"]:
        lines.append(f"| `{check['command']}` | {check['returncode']} | `{check['passed']}` |")
    lines.extend(["", "## Pass / Fail", "", result["pass_fail"]["reason"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = build_repair_evidence()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
