#!/usr/bin/env python3
"""Generate deterministic E08 control-behavior matrix evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "e08_control_behavior_matrix_v0.2.66"


def build_matrix() -> dict[str, Any]:
    harness = PlatformHarness(
        storage=Storage(ROOT / ".tmp" / "v02_66_e08_matrix"),
        network_egress_policy="none",
        network_egress_allowlist=["api.example.test"],
        secret_policy_enabled=True,
        worker_lease_seconds=60,
    )
    boundary = harness.policy_controls()["e08_boundary"]
    return {
        "version": "v0.2.66",
        "current_slice": boundary["current_slice"],
        "source": boundary["source"],
        "not_full_sidecar_completion": boundary["not_full_sidecar_completion"],
        "behavior_matrix": boundary["behavior_matrix"],
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(matrix: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(matrix, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# E08 control-behavior matrix v0.2.66",
        "",
        f"- Raw matrix: `{relative(json_path)}`",
        f"- Current slice: `{matrix['current_slice']}`",
        f"- Not full sidecar completion: `{matrix['not_full_sidecar_completion']}`",
        "",
        "| Control | Layer | Enforcement | Status | Source |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in matrix["behavior_matrix"]:
        lines.append(
            f"| `{row['id']}` | {row['layer']} | {row['enforcement']} | {row['status']} | `{row['source']}` |"
        )
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    matrix = build_matrix()
    json_path, summary_path = write_outputs(matrix, args.output_dir)
    print(json_path)
    print(summary_path)


if __name__ == "__main__":
    main()
