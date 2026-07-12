#!/usr/bin/env python3
"""Generate v0.2.89 limited default enablement contract evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import classify_requirement, limited_default_enablement_plan_status

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from frontend_verification_runner import ROOT, run_frontend_verification


OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "contract_v0.2.89_complexity_router_limited_default_enablement"


def build_contract_evidence() -> dict[str, Any]:
    disabled_status = limited_default_enablement_plan_status()
    enabled_status = limited_default_enablement_plan_status(
        default_mode="limited_default",
        limited_default_enabled=True,
        min_confidence=0.55,
    )
    disabled_classification = classify_requirement("Fix a typo in a settings label")
    enabled_classification = classify_requirement(
        "Fix a typo in a settings label",
        default_mode="limited_default",
        limited_default_enabled=True,
        min_confidence=0.55,
    )
    unknown_classification = classify_requirement(
        "",
        default_mode="limited_default",
        limited_default_enabled=True,
        min_confidence=0.55,
    )
    frontend = run_frontend_verification()
    passed = (
        disabled_status["default_enabled"] is False
        and disabled_classification["default_router_enabled"] is False
        and enabled_status["default_enabled"] is True
        and enabled_classification["default_router_enabled"] is True
        and unknown_classification["default_router_enabled"] is False
        and frontend["passed"] is True
    )
    return {
        "version": "v0.2.89",
        "contract_id": "complexity_router_limited_default_enablement_contract",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.88_complexity_router_limited_default_enablement_plan.md",
        "status": "completed" if passed else "failed",
        "default_settings_status": disabled_status,
        "explicit_limited_default_status": enabled_status,
        "default_settings_classification": disabled_classification,
        "explicit_limited_default_classification": enabled_classification,
        "unknown_limited_default_classification": unknown_classification,
        "frontend_verification": frontend,
        "pass_fail": {
            "passed": passed,
            "reason": "limited default contract implemented and default settings remain disabled"
            if passed
            else "limited default contract criteria failed",
        },
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
    lines = [
        "# v0.2.89 complexity-router limited default enablement contract",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Reason: {result['pass_fail']['reason']}",
        f"- Default settings mode: `{result['default_settings_status']['configured_default_mode']}`",
        f"- Default settings enabled: `{result['default_settings_status']['default_enabled']}`",
        f"- Explicit limited default enabled: `{result['explicit_limited_default_status']['default_enabled']}`",
        f"- Default settings classification router enabled: `{result['default_settings_classification']['default_router_enabled']}`",
        f"- Explicit limited default classification router enabled: `{result['explicit_limited_default_classification']['default_router_enabled']}`",
        f"- Unknown limited default router enabled: `{result['unknown_limited_default_classification']['default_router_enabled']}`",
        f"- Frontend verification passed: `{result['frontend_verification']['passed']}`",
        "",
        "## API / Config Contract",
        "",
        "- Settings default mode remains `disabled`.",
        "- Explicit `limited_default` mode can surface `default_builder_policy` for eligible classifications.",
        "- Unknown requirements remain complex-equivalent and not default-router-enabled.",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = build_contract_evidence()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
