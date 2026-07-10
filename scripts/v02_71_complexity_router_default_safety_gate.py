#!/usr/bin/env python3
"""Generate v0.2.71 complexity-router default-safety gate evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "default_safety_v0.2.71_complexity_router"


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(status: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.71 complexity-router default-safety gate",
        "",
        f"- Raw status: `{relative(json_path)}`",
        f"- Default enabled: `{status['default_enabled']}`",
        f"- Allowed to enable default: `{status['allowed_to_enable_default']}`",
        f"- Router ready for default: `{status['router_ready_for_default']}`",
        f"- Reason: {status['reason']}",
        "",
        "| Prerequisite | Satisfied | Required for default |",
        "| --- | --- | --- |",
    ]
    for item in status["prerequisites"]:
        lines.append(f"| `{item['id']}` | `{item['satisfied']}` | `{item['required_for_default']}` |")
    lines.extend([
        "",
        "## Missing prerequisites",
        "",
        ", ".join(f"`{item}`" for item in status["missing_prerequisites"]) or "none",
        "",
        "## Supporting guardrails",
        "",
        ", ".join(f"`{item}`" for item in status["supporting_guardrails"]),
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    status = complexity_router_default_safety_gate()
    json_path, summary_path = write_outputs(status)
    print(json_path)
    print(summary_path)
    print(status["allowed_to_enable_default"])


if __name__ == "__main__":
    main()
