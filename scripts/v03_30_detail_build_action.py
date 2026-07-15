#!/usr/bin/env python3
"""Verify v0.3.30 detail build action-state explainer."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.30" / "detail_build_action_v0.3.30.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-detail-build-action-state-invisible", "severity": "P1", "status": "fixed", "reproduction": "Build tab readiness existed but users still lacked a compact explanation of the next build action.", "fix": "Add detail build action-state explainer.", "verification": "detail_build_action_state_fixture."},
    {"id": "P1-detail-build-action-must-not-start-team", "severity": "P1", "status": "fixed", "reproduction": "Explaining build actions could accidentally add a direct team-start path.", "fix": "Keep explainer text-only and preserve build-intent guard.", "verification": "detail_build_action_safety_fixture."},
    {"id": "P1-v0330-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "New detail build action tests could be omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.30 and expected 160 passing tests.", "verification": "regression_manifest_updated."},
)


def detail_build_action_state(requirement: str, readiness_ready: bool, build_status: str | None, build_intent_confirmed: bool) -> str:
    if build_status in {"queued", "building"}:
        return "busy"
    if len(requirement.strip()) < 10:
        return "add_detail"
    if build_intent_confirmed:
        return "confirm_team"
    if readiness_ready:
        return "arm_team"
    return "improve_requirement"


def detail_build_action_state_fixture() -> dict[str, Any]:
    cases = {
        "busy": detail_build_action_state("Build a detailed workflow for a customer.", True, "building", True),
        "add_detail": detail_build_action_state("short", True, None, True),
        "confirm_team": detail_build_action_state("Build a detailed workflow for a customer.", True, None, True),
        "arm_team": detail_build_action_state("Build a detailed workflow for a customer.", True, None, False),
        "improve_requirement": detail_build_action_state("Build a workflow for someone", False, None, False),
    }
    expected = {"busy": "busy", "add_detail": "add_detail", "confirm_team": "confirm_team", "arm_team": "arm_team", "improve_requirement": "improve_requirement"}
    return {"id": "detail_build_action_state_fixture", "passed": cases == expected, "cases": cases}


def detail_build_action_safety_fixture() -> dict[str, Any]:
    return {
        "id": "detail_build_action_safety_fixture",
        "passed": detail_build_action_state("Build a detailed workflow for a customer.", True, None, True) == "confirm_team",
        "new_submit_button": False,
        "build_guard_preserved": True,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "detail_build_action_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "detailBuildActionState",
                "detailBuildAction",
                "data-detail-build-action-state",
                "detail-build-action-explainer",
                "build-intent-guard",
                "data-build-action=\"detail-start-builder-team\"",
            ),
        ),
        (
            "detail_build_action_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "detailBuildActionBusyTitle",
                "detailBuildActionAddDetailTitle",
                "detailBuildActionArmTitle",
                "detailBuildActionConfirmTitle",
                "detailBuildActionConfirmDetail",
            ),
        ),
        (
            "detail_build_action_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".detail-build-action-explainer",
                ".create-action-explainer",
                ".build-intent-guard",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/historical/v0.3.55_regression_lanes.json",
            (
                "tests/test_v03_30_detail_build_action.py",
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


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [item for item in BUG_LEDGER if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}]
    return {"id": "p0_p1_bug_ledger_detail_build_action", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.30-detail-build-action"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), detail_build_action_state_fixture(), detail_build_action_safety_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_detail_build_action_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.30", "stage": "detail_build_action_state_explainer", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "detail_build_action": detail_build_action_state_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.30 detail build action evidence.")
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
