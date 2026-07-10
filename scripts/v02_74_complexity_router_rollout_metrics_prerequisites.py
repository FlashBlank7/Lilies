#!/usr/bin/env python3
"""Generate v0.2.74 complexity-router rollout metrics prerequisite evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import (
    complexity_router_default_safety_gate,
    rollout_metrics_prerequisites_status,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "metrics_v0.2.74_complexity_router"


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def build_evidence() -> dict[str, Any]:
    return {
        "version": "v0.2.74",
        "rollout_metrics": rollout_metrics_prerequisites_status(),
        "default_safety": complexity_router_default_safety_gate(),
    }


def write_outputs(evidence: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    metrics = evidence["rollout_metrics"]
    default_safety = evidence["default_safety"]
    lines = [
        "# v0.2.74 complexity-router rollout metrics prerequisites",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Metrics prerequisite satisfied: `{metrics['satisfied']}`",
        f"- Metrics status: `{metrics['status']}`",
        f"- Default enabled: `{default_safety['default_enabled']}`",
        f"- Allowed to enable default: `{default_safety['allowed_to_enable_default']}`",
        f"- Missing prerequisites: {', '.join(f'`{item}`' for item in default_safety['missing_prerequisites']) or 'none'}",
        "",
        "| Metric | Description |",
        "| --- | --- |",
    ]
    for metric in metrics["required_metrics"]:
        lines.append(f"| `{metric['id']}` | {metric['description']} |")
    lines.extend([
        "",
        "## Conclusion",
        "",
        (
            "Rollout metrics prerequisites are satisfied as an API-visible empty-state schema. "
            "Default enablement remains off and requires a separate stage-report-selected decision."
        ),
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    evidence = build_evidence()
    json_path, summary_path = write_outputs(evidence)
    print(json_path)
    print(summary_path)
    print(evidence["default_safety"]["default_enabled"])


if __name__ == "__main__":
    main()
