#!/usr/bin/env python3
"""Decide the complexity-router default enablement boundary after guardrails."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.75_complexity_router_enablement_boundary"


@dataclass(frozen=True)
class EnablementOption:
    option_id: str
    label: str
    safety: int
    evidence_readiness: int
    product_value: int
    default_change_risk: int
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return self.safety + self.evidence_readiness + self.product_value - self.default_change_risk

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "safety": self.safety,
            "evidence_readiness": self.evidence_readiness,
            "product_value": self.product_value,
            "default_change_risk": self.default_change_risk,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    EnablementOption(
        option_id="require_live_validation_before_default_change",
        label="Require live validation before any default change",
        safety=5,
        evidence_readiness=4,
        product_value=4,
        default_change_risk=1,
        disposition="selected",
        next_version="v0.2.76_complexity_router_live_validation_plan",
        first_design="docs/current-design/design_complexity_router_live_validation_plan.md",
    ),
    EnablementOption(
        option_id="enter_enablement_review_now",
        label="Enter default enablement review now",
        safety=3,
        evidence_readiness=3,
        product_value=5,
        default_change_risk=4,
        disposition="deferred until live validation plan exists",
        next_version="v0.2.x_complexity_router_enablement_review",
        first_design="docs/current-design/design_complexity_router_enablement_review.md",
    ),
    EnablementOption(
        option_id="defer_enablement_indefinitely",
        label="Defer default enablement indefinitely",
        safety=5,
        evidence_readiness=2,
        product_value=1,
        default_change_risk=0,
        disposition="rejected because it loses the value of completed guardrails without adding evidence",
        next_version="v0.2.x_complexity_router_deferred",
        first_design="docs/current-design/design_complexity_router_deferred.md",
    ),
]


def decide_enablement_boundary() -> dict[str, Any]:
    ranked = sorted(OPTIONS, key=lambda option: (-option.score, option.option_id))
    decision = ranked[0]
    safety = complexity_router_default_safety_gate()
    return {
        "version": "v0.2.75",
        "decision": decision.to_json(),
        "options": [option.to_json() for option in ranked],
        "default_safety": safety,
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "conclusion": (
            "Default-safety prerequisites are satisfied, but default behavior remains disabled. "
            "Require a live validation plan before any default change."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(decision: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.75 complexity-router default enablement boundary",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
        f"- Default enabled: `{decision['default_enabled']}`",
        f"- Allowed to enable default: `{decision['allowed_to_enable_default']}`",
        f"- Next version: `{decision['decision']['next_version']}`",
        f"- First design: `{decision['decision']['first_design']}`",
        "",
        "| Option | Score | Disposition |",
        "| --- | ---: | --- |",
    ]
    for option in decision["options"]:
        lines.append(f"| `{option['option_id']}` | {option['score']} | {option['disposition']} |")
    lines.extend(["", "## Conclusion", "", decision["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    decision = decide_enablement_boundary()
    json_path, summary_path = write_outputs(decision)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
