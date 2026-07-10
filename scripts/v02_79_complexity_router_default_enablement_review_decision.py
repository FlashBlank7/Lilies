#!/usr/bin/env python3
"""Decide the post-live-validation complexity-router enablement path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
LIVE_EVIDENCE_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.78" / "live_v0.2.78_complexity_router_bounded_validation.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.79_complexity_router_default_enablement_review"


@dataclass(frozen=True)
class EnablementReviewOption:
    option_id: str
    label: str
    safety: int
    live_evidence: int
    product_value: int
    rollout_readiness: int
    frontend_operator_risk: int
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return self.safety + self.live_evidence + self.product_value + self.rollout_readiness - self.frontend_operator_risk

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "safety": self.safety,
            "live_evidence": self.live_evidence,
            "product_value": self.product_value,
            "rollout_readiness": self.rollout_readiness,
            "frontend_operator_risk": self.frontend_operator_risk,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    EnablementReviewOption(
        option_id="prepare_staged_rollout",
        label="Prepare staged rollout before default enablement",
        safety=5,
        live_evidence=5,
        product_value=4,
        rollout_readiness=4,
        frontend_operator_risk=1,
        disposition="selected",
        next_version="v0.2.80_complexity_router_staged_rollout_preparation",
        first_design="docs/current-design/design_complexity_router_staged_rollout_preparation.md",
    ),
    EnablementReviewOption(
        option_id="enter_immediate_enablement_review",
        label="Enter immediate default enablement review",
        safety=3,
        live_evidence=5,
        product_value=5,
        rollout_readiness=2,
        frontend_operator_risk=4,
        disposition="deferred until staged rollout preparation exists and frontend verification is restored or explicitly waived",
        next_version="v0.2.x_complexity_router_immediate_enablement_review",
        first_design="docs/current-design/design_complexity_router_immediate_enablement_review.md",
    ),
    EnablementReviewOption(
        option_id="continue_deferral",
        label="Continue deferral despite passed live validation",
        safety=5,
        live_evidence=5,
        product_value=1,
        rollout_readiness=1,
        frontend_operator_risk=0,
        disposition="rejected because it wastes completed guardrail and live evidence without adding a safer rollout path",
        next_version="v0.2.x_complexity_router_enablement_deferral",
        first_design="docs/current-design/design_complexity_router_enablement_deferral.md",
    ),
]


def load_live_evidence() -> dict[str, Any]:
    return json.loads(LIVE_EVIDENCE_PATH.read_text(encoding="utf-8"))


def decide_enablement_review() -> dict[str, Any]:
    evidence = load_live_evidence()
    safety = complexity_router_default_safety_gate()
    ranked = sorted(OPTIONS, key=lambda option: (-option.score, option.option_id))
    decision = ranked[0]
    return {
        "version": "v0.2.79",
        "source_live_evidence": LIVE_EVIDENCE_PATH.relative_to(ROOT).as_posix(),
        "decision": decision.to_json(),
        "options": [option.to_json() for option in ranked],
        "live_evidence": {
            "status": evidence["status"],
            "provider": evidence["provider"],
            "model": evidence["model"],
            "passed": evidence["pass_fail"]["passed"],
            "case_count": len(evidence["case_results"]),
        },
        "default_safety": safety,
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "conclusion": (
            "Prepare staged rollout before any default enablement. This preserves the positive live evidence "
            "while avoiding an immediate default behavior change."
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
        "# v0.2.79 complexity-router default enablement review decision",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
        f"- Live evidence: `{decision['live_evidence']['status']}` / passed `{decision['live_evidence']['passed']}`",
        f"- Provider/model: `{decision['live_evidence']['provider']}` / `{decision['live_evidence']['model']}`",
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
    decision = decide_enablement_review()
    json_path, summary_path = write_outputs(decision)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
