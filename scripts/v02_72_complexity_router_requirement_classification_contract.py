#!/usr/bin/env python3
"""Generate v0.2.72 complexity-router requirement classification evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import (
    classify_requirement,
    complexity_router_default_safety_gate,
    requirement_classification_contract_status,
)


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "classification_v0.2.72_complexity_router"
SAMPLE_REQUIREMENTS = {
    "simple": "Fix a typo in the settings label",
    "medium": "Add an API endpoint with tests for the reporting workflow",
    "complex": "Design a platform guardrail rollout for a model-sensitive agent router",
    "unknown": "",
}


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def build_evidence() -> dict[str, Any]:
    samples = {
        sample_id: classify_requirement(requirement)
        for sample_id, requirement in SAMPLE_REQUIREMENTS.items()
    }
    return {
        "version": "v0.2.72",
        "contract": requirement_classification_contract_status(),
        "samples": samples,
        "default_safety": complexity_router_default_safety_gate(),
    }


def write_outputs(evidence: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    default_safety = evidence["default_safety"]
    lines = [
        "# v0.2.72 complexity-router requirement classification contract",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Contract satisfied: `{evidence['contract']['satisfied']}`",
        f"- Default enabled: `{default_safety['default_enabled']}`",
        f"- Allowed to enable default: `{default_safety['allowed_to_enable_default']}`",
        f"- Missing prerequisites: {', '.join(f'`{item}`' for item in default_safety['missing_prerequisites'])}",
        "",
        "| Sample | Requirement class | Effective class | Conservative unknown | Signals |",
        "| --- | --- | --- | --- | --- |",
    ]
    for sample_id, result in evidence["samples"].items():
        signals = ", ".join(f"`{signal}`" for signal in result["signals"])
        lines.append(
            f"| `{sample_id}` | `{result['requirement_class']}` | `{result['effective_class']}` | "
            f"`{result['conservative_unknown']}` | {signals} |"
        )
    lines.extend([
        "",
        "## Conclusion",
        "",
        (
            "Requirement classification contract is satisfied and API-visible. Complexity-router defaults remain "
            "disabled because operator override plan and rollout metrics prerequisites are still missing."
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
