#!/usr/bin/env python3
"""Decide the post-operator-opt-in productization path for the complexity router."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
OPERATOR_OPT_IN_EVIDENCE_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.84" / "rollout_v0.2.84_complexity_router_operator_opt_in.json"
FRONTEND_DIR = ROOT / "platform" / "frontend"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.85_complexity_router_post_operator_opt_in"


@dataclass(frozen=True)
class ProductizationPathOption:
    option_id: str
    label: str
    safety: int
    productization_value: int
    evidence_readiness: int
    verification_readiness: int
    behavior_change_risk: int
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return (
            self.safety
            + self.productization_value
            + self.evidence_readiness
            + self.verification_readiness
            - self.behavior_change_risk
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "safety": self.safety,
            "productization_value": self.productization_value,
            "evidence_readiness": self.evidence_readiness,
            "verification_readiness": self.verification_readiness,
            "behavior_change_risk": self.behavior_change_risk,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    ProductizationPathOption(
        option_id="repair_frontend_verification_environment",
        label="Repair frontend verification environment",
        safety=5,
        productization_value=5,
        evidence_readiness=5,
        verification_readiness=1,
        behavior_change_risk=0,
        disposition="selected",
        next_version="v0.2.86_frontend_verification_environment_repair",
        first_design="docs/current-design/design_frontend_verification_environment_repair.md",
    ),
    ProductizationPathOption(
        option_id="continue_operator_opt_in_observation",
        label="Continue operator opt-in observation",
        safety=5,
        productization_value=2,
        evidence_readiness=4,
        verification_readiness=1,
        behavior_change_risk=0,
        disposition="rejected because stage_1 exit criteria are already satisfied and productization is blocked by frontend verification",
        next_version="v0.2.x_complexity_router_continued_operator_opt_in_observation",
        first_design="docs/current-design/design_complexity_router_continued_operator_opt_in_observation.md",
    ),
    ProductizationPathOption(
        option_id="begin_default_enablement_review",
        label="Begin default enablement review",
        safety=2,
        productization_value=5,
        evidence_readiness=5,
        verification_readiness=0,
        behavior_change_risk=4,
        disposition="rejected because executable frontend verification is still blocked",
        next_version="v0.2.x_complexity_router_default_enablement_review",
        first_design="docs/current-design/design_complexity_router_default_enablement_review.md",
    ),
]


def load_operator_opt_in_evidence() -> dict[str, Any]:
    return json.loads(OPERATOR_OPT_IN_EVIDENCE_PATH.read_text(encoding="utf-8"))


def frontend_environment_probe() -> dict[str, Any]:
    commands = {
        "node": shutil.which("node"),
        "npm": shutil.which("npm"),
        "brew": shutil.which("brew"),
        "corepack": shutil.which("corepack"),
        "nvm": shutil.which("nvm"),
        "volta": shutil.which("volta"),
        "fnm": shutil.which("fnm"),
    }
    return {
        "frontend_dir": FRONTEND_DIR.relative_to(ROOT).as_posix(),
        "package_json_present": (FRONTEND_DIR / "package.json").exists(),
        "package_lock_present": (FRONTEND_DIR / "package-lock.json").exists(),
        "node_modules_present": (FRONTEND_DIR / "node_modules").exists(),
        "commands": {name: bool(path) for name, path in commands.items()},
        "command_paths": {name: path for name, path in commands.items() if path},
        "executable_frontend_verification_available": bool(commands["node"] and commands["npm"]),
    }


def decide_post_operator_opt_in_path() -> dict[str, Any]:
    evidence = load_operator_opt_in_evidence()
    probe = frontend_environment_probe()
    safety = complexity_router_default_safety_gate()
    stage_1_passed = bool(evidence["pass_fail"]["passed"]) and evidence["default_enabled"] is False
    ranked = sorted(OPTIONS, key=lambda option: (-option.score, option.option_id))
    decision = ranked[0]
    return {
        "version": "v0.2.85",
        "decision_id": "complexity_router_post_operator_opt_in_decision",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.84_complexity_router_operator_opt_in_rollout.md",
        "source_operator_opt_in_evidence": OPERATOR_OPT_IN_EVIDENCE_PATH.relative_to(ROOT).as_posix(),
        "stage_1_passed": stage_1_passed,
        "operator_opt_in_metrics": evidence["metrics"],
        "frontend_environment": probe,
        "decision": decision.to_json(),
        "options": [option.to_json() for option in ranked],
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "default_enablement_review_deferred_until_frontend_verification": True,
        "conclusion": "Repair frontend verification before default enablement review. Defaults remain disabled.",
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
    env = decision["frontend_environment"]
    lines = [
        "# v0.2.85 complexity-router post-operator-opt-in decision",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
        f"- Stage 1 passed: `{decision['stage_1_passed']}`",
        f"- Frontend verification available: `{env['executable_frontend_verification_available']}`",
        f"- Node available: `{env['commands']['node']}`",
        f"- npm available: `{env['commands']['npm']}`",
        f"- package.json present: `{env['package_json_present']}`",
        f"- node_modules present: `{env['node_modules_present']}`",
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
    decision = decide_post_operator_opt_in_path()
    json_path, summary_path = write_outputs(decision)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
