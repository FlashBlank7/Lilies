#!/usr/bin/env python3
"""Generate v0.2.136 E10 governed memory boundary definition evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "boundary_v0.2.136_e10_governed_memory"


REQUIRED_CONTROLS = [
    "permission_scope",
    "audit_log",
    "revoke",
    "retention_policy",
    "source_attribution",
    "no_unrestricted_filesystem_memory",
]


def boundary() -> dict[str, Any]:
    controls = {
        "permission_scope": {
            "required": True,
            "rule": "Memory write/read requires explicit user or operator-scoped permission.",
        },
        "audit_log": {
            "required": True,
            "rule": "Every create/read/update/revoke/expire operation records actor, source, reason, and timestamp.",
        },
        "revoke": {
            "required": True,
            "rule": "A revoked memory item must be excluded from retrieval and retained only as redacted audit metadata.",
        },
        "retention_policy": {
            "required": True,
            "rule": "Every memory item has a retention class and expires unless explicitly renewed under policy.",
        },
        "source_attribution": {
            "required": True,
            "rule": "Every memory item stores source type, source id, captured_at, and evidence text/hash.",
        },
        "no_unrestricted_filesystem_memory": {
            "required": True,
            "rule": "The surface must not index arbitrary filesystem paths or background activity without scoped permission.",
        },
    }
    missing = [name for name in REQUIRED_CONTROLS if name not in controls or not controls[name]["required"]]
    return {
        "version": "v0.2.136",
        "boundary_id": "e10_governed_memory_boundary",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.135_blocked_experiment_resolution_selection.md",
        "status": "completed" if not missing else "needs_attention",
        "controls": controls,
        "required_controls": REQUIRED_CONTROLS,
        "missing_controls": missing,
        "accepted_product_scope": not missing,
        "unrestricted_memory_allowed": False,
        "filesystem_wrapper_allowed": False,
        "e02_true_human_panel_resolved": False,
        "next_version": "v0.2.137_e10_governed_memory_surface_contract",
        "first_design": "docs/current-design/design_v0_2_137_e10_governed_memory_surface_contract.md",
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
        "# v0.2.136 E10 governed memory boundary",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Accepted product scope: `{result['accepted_product_scope']}`",
        f"- Unrestricted memory allowed: `{result['unrestricted_memory_allowed']}`",
        f"- Filesystem wrapper allowed: `{result['filesystem_wrapper_allowed']}`",
        f"- Missing controls: `{len(result['missing_controls'])}`",
        f"- Next version: `{result['next_version']}`",
        "",
        "## Controls",
        "",
    ]
    for name in result["required_controls"]:
        lines.append(f"- `{name}`: {result['controls'][name]['rule']}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = boundary()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
