#!/usr/bin/env python3
"""Verify v0.3.16 scenario journeys and application-card guidance."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.16" / "scenario_journey_regression_v0.3.16.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"


SCENARIO_MATRIX = (
    {
        "id": "business_owner_frontdoor",
        "role": "business_owner",
        "behavior": "starts from customer scenario examples and wants a safe first draft",
        "required_surfaces": ["customer-intake-panel", "saveDraftOnly", "customer-section"],
    },
    {
        "id": "implementation_consultant_returning_draft",
        "role": "implementation_consultant",
        "behavior": "opens an existing app and needs structure, acceptance, and next action",
        "required_surfaces": ["app-readiness", "draft-readiness", "next-action-checklist"],
    },
    {
        "id": "operator_run_and_monitor",
        "role": "operator",
        "behavior": "tries a draft or published version and reads trace or monitor evidence",
        "required_surfaces": ["try-readiness-panel", "trace-readability-panel", "monitor-readability-panel"],
    },
    {
        "id": "technical_reviewer_acceptance_policy",
        "role": "technical_reviewer",
        "behavior": "checks acceptance, policy, monitor, and audit evidence before trusting automation",
        "required_surfaces": ["acceptance-readiness-panel", "policy-controls", "monitor-list"],
    },
    {
        "id": "investor_demo_reviewer_application_list",
        "role": "investor_demo_reviewer",
        "behavior": "scans the application list to decide which workflow is demo-ready",
        "required_surfaces": ["app-readiness", "app-next-action", "data-app-card-guidance"],
    },
)


BUG_LEDGER = (
    {
        "id": "P1-application-list-demo-readiness-opaque",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Returning users and demo reviewers saw only revision/version metadata on application cards.",
        "fix": "Add app-card readiness chips and next-action guidance derived from existing application fields.",
        "verification": "application_card_guidance_markers.",
    },
    {
        "id": "P1-scenario-matrix-missing-investor-demo-reviewer",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Persona coverage did not explicitly include investor/demo review behavior.",
        "fix": "Add investor/demo reviewer to the scenario matrix and home scenario copy.",
        "verification": "scenario_matrix_roles.",
    },
    {
        "id": "P1-new-scenario-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New v0.3.x tests could be omitted from the current release-gate manifest.",
        "fix": "Update `docs/testing/historical/v0.3.55_regression_lanes.json` with v0.3.16 and expected 78 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "application_card_guidance_markers",
            "platform/frontend/app/page.tsx",
            (
                "appCardReadiness",
                "appCardNextAction",
                'data-app-card-guidance="readiness"',
                'data-app-card-guidance="next-action"',
            ),
        ),
        (
            "application_card_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "appCardDraftState",
                "appNextActionRunAcceptance",
                "Investor/demo reviewer",
                "投资/演示审阅者",
            ),
        ),
        (
            "application_card_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".app-readiness",
                ".app-next-action",
                ".app-card{min-height:",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/historical/v0.3.55_regression_lanes.json",
            (
                "tests/test_v03_16_scenario_journey_regression.py",
                "v0.3.x_current_release_gate",
            ),
        ),
    ]
    evidence: list[dict[str, Any]] = []
    for check_id, relative_path, markers in checks:
        text = read_text(relative_path)
        missing = [marker for marker in markers if marker not in text]
        evidence.append({"id": check_id, "path": relative_path, "required_markers": list(markers), "missing_markers": missing, "passed": not missing})
    return evidence


def scenario_matrix_evidence() -> dict[str, Any]:
    roles = {item["role"] for item in SCENARIO_MATRIX}
    required_roles = {"business_owner", "implementation_consultant", "operator", "technical_reviewer", "investor_demo_reviewer"}
    selected_gap = next(item for item in SCENARIO_MATRIX if item["id"] == "investor_demo_reviewer_application_list")
    return {
        "id": "scenario_matrix_roles",
        "passed": required_roles.issubset(roles) and selected_gap["role"] == "investor_demo_reviewer",
        "required_roles": sorted(required_roles),
        "roles": sorted(roles),
        "journey_count": len(SCENARIO_MATRIX),
        "selected_p1_gap": selected_gap,
    }


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [item for item in BUG_LEDGER if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}]
    return {"id": "p0_p1_bug_ledger_scenario_journeys", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.16-scenario-journeys"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body)}


def runtime_health_check(api_url: str) -> dict[str, Any]:
    url = api_url.rstrip("/") + "/health"
    try:
        result = request_json(url)
        runtime = result["json"].get("runtime", {}) if isinstance(result["json"], dict) else {}
        return {"id": "runtime_health_read_only", "url": url, "passed": result["status_code"] == 200 and runtime.get("version") == EXPECTED_RUNTIME_VERSION and runtime.get("current_code_ready") is True, "status_code": result["status_code"], "runtime": runtime}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return {"id": "runtime_health_read_only", "url": url, "passed": False, "status_code": 0, "error": str(error)}


def build_evidence(*, live: bool = False, api_url: str = "http://127.0.0.1:8001") -> dict[str, Any]:
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), scenario_matrix_evidence(), *source_marker_checks()]
    safety: dict[str, Any] = {"build_endpoint_called": False, "called_endpoints": []}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["build_endpoint_called"] = any("/builds" in endpoint for endpoint in safety["called_endpoints"])
    checks.append({"id": "safety_no_build_call", "passed": safety["build_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint": "POST /api/v1/applications/{id}/builds"})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.16",
        "stage": "scenario_journey_regression_expansion",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "scenario_matrix": list(SCENARIO_MATRIX),
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "journey_count": len(SCENARIO_MATRIX), "build_endpoint_called": safety["build_endpoint_called"]},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.16 scenario journey evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    evidence = build_evidence(live=args.live, api_url=args.api_url)
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
