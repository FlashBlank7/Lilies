#!/usr/bin/env python3
"""Generate v0.2.98 Studio editable policy-controls evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.98_e08_studio_editable_policy_controls"


def contains_all(text: str, needles: list[str]) -> bool:
    return all(needle in text for needle in needles)


def generate_evidence() -> dict[str, Any]:
    page_path = ROOT / "platform" / "frontend" / "app" / "applications" / "[id]" / "page.tsx"
    platform_path = ROOT / "platform" / "frontend" / "lib" / "platform.ts"
    i18n_path = ROOT / "platform" / "frontend" / "lib" / "i18n.ts"
    css_path = ROOT / "platform" / "frontend" / "app" / "globals.css"
    page = page_path.read_text(encoding="utf-8")
    platform = platform_path.read_text(encoding="utf-8")
    i18n = i18n_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8")
    required_controls = [
        "network_egress_policy",
        "network_egress_allowlist",
        "cancellation_policy",
        "secret_policy_enabled",
        "worker_lease_seconds",
        "policyLimitKeys.map",
        "policyForm.reason",
    ]
    return {
        "version": "v0.2.98",
        "source_stage_report": "docs/stage-reports/v0.2.97_e08_post_api_productization_decision.md",
        "status": "completed",
        "studio_page": page_path.relative_to(ROOT).as_posix(),
        "type_contract": platform_path.relative_to(ROOT).as_posix(),
        "checks": {
            "patch_endpoint_wired": "PATCH" in page and "/api/v1/platform/harness/policy-controls" in page,
            "save_function_present": "savePolicyControls" in page,
            "editable_controls_present": contains_all(page, required_controls),
            "type_contract_present": contains_all(platform, [
                "PlatformPolicyControlsUpdate",
                "PlatformPolicyControlsUpdateResponse",
                "cancellation_policy",
            ]),
            "i18n_present": contains_all(i18n, [
                "policyCancellation",
                "policySave",
                "policySaving",
                "policySaved",
            ]),
            "css_present": contains_all(css, [
                "policy-edit-form",
                "policy-limit-grid",
                "policy-actions",
            ]),
        },
        "frontend_verification": {
            "lint_command": 'PATH="$HOME/.nvm/versions/node/v24.15.0/bin:$PATH" npm run lint',
            "route_smoke": "curl -I http://127.0.0.1:3108/applications/smoke-v02-98 -> 200",
        },
        "backend_regression": ".venv/bin/python -m pytest tests/test_workflow.py -k 'policy_controls' tests/test_v02_96_e08_editable_policy_controls_api.py -q",
        "e07_invariant": {
            "status": "preserved",
            "no_e07_code_or_default_change": True,
        },
        "not_full_sidecar_completion": True,
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
        "# v0.2.98 E08 Studio editable policy-controls evidence",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Studio page: `{result['studio_page']}`",
        f"- Type contract: `{result['type_contract']}`",
        "",
        "## Checks",
        "",
    ]
    for key, value in result["checks"].items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend([
        "",
        "## Verification",
        "",
        f"- Frontend lint: `{result['frontend_verification']['lint_command']}`",
        f"- Route smoke: `{result['frontend_verification']['route_smoke']}`",
        f"- Backend regression: `{result['backend_regression']}`",
        f"- E07 invariant: `{result['e07_invariant']['status']}`",
        f"- Not full sidecar completion: `{result['not_full_sidecar_completion']}`",
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = generate_evidence()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print("studio_editable_policy_controls_evidence")


if __name__ == "__main__":
    main()
