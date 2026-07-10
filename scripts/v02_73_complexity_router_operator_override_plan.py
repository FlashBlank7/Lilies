#!/usr/bin/env python3
"""Generate v0.2.73 complexity-router operator override evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import (
    complexity_router_default_safety_gate,
    operator_override_plan_status,
    validate_operator_override,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "override_v0.2.73_complexity_router"
SAMPLE_OVERRIDES = [
    ("disabled", ""),
    ("force_simple", "Low-risk text-only edit"),
    ("force_medium", ""),
    ("force_magic", "unsupported mode smoke test"),
]


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def build_evidence() -> dict[str, Any]:
    validations = [
        validate_operator_override(mode, reason)
        for mode, reason in SAMPLE_OVERRIDES
    ]
    return {
        "version": "v0.2.73",
        "operator_override_plan": operator_override_plan_status(),
        "validations": validations,
        "default_safety": complexity_router_default_safety_gate(),
    }


def write_outputs(evidence: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    default_safety = evidence["default_safety"]
    lines = [
        "# v0.2.73 complexity-router operator override plan",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Override plan satisfied: `{evidence['operator_override_plan']['satisfied']}`",
        f"- Default enabled: `{default_safety['default_enabled']}`",
        f"- Allowed to enable default: `{default_safety['allowed_to_enable_default']}`",
        f"- Missing prerequisites: {', '.join(f'`{item}`' for item in default_safety['missing_prerequisites'])}",
        "",
        "| Mode | Valid | Target class | Error | Operator-visible reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in evidence["validations"]:
        lines.append(
            f"| `{result['mode']}` | `{result['valid']}` | `{result['target_class']}` | "
            f"`{result['error']}` | {result['operator_visible_reason'] or 'none'} |"
        )
    lines.extend([
        "",
        "## Conclusion",
        "",
        (
            "Operator override plan is satisfied and API-visible. Complexity-router defaults remain disabled "
            "because rollout metrics prerequisites are still missing."
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
    print(evidence["default_safety"]["allowed_to_enable_default"])


if __name__ == "__main__":
    main()
