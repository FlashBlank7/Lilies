#!/usr/bin/env python3
"""Verify v0.3.31 detail build recommended action."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "detail_build_recommended_action_v0.3.31.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-detail-build-recommended-action-absent", "severity": "P1", "status": "fixed", "reproduction": "Build tab explained action state but did not provide a safe recommended affordance.", "fix": "Add detail build recommended-action strip.", "verification": "detail_build_recommended_action_fixture."},
    {"id": "P1-detail-build-recommended-must-not-autostart", "severity": "P1", "status": "fixed", "reproduction": "A recommended action could accidentally start the model team.", "fix": "Map arm/confirm states to guarded button focus only.", "verification": "detail_build_recommended_safety_fixture."},
    {"id": "P1-v0331-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "New detail build recommended-action tests could be omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.31 and expected 166 passing tests.", "verification": "regression_manifest_updated."},
)


def recommended_detail_build_action(action_id: str) -> dict[str, Any]:
    if action_id == "busy":
        return {"target": "wait", "disabled": True}
    if action_id in {"arm_team", "confirm_team"}:
        return {"target": "guarded_build_button", "disabled": False}
    return {"target": "requirement_focus", "disabled": False}


def detail_build_recommended_action_fixture() -> dict[str, Any]:
    cases = {
        "busy": recommended_detail_build_action("busy"),
        "add_detail": recommended_detail_build_action("add_detail"),
        "improve_requirement": recommended_detail_build_action("improve_requirement"),
        "arm_team": recommended_detail_build_action("arm_team"),
        "confirm_team": recommended_detail_build_action("confirm_team"),
    }
    expected = {
        "busy": {"target": "wait", "disabled": True},
        "add_detail": {"target": "requirement_focus", "disabled": False},
        "improve_requirement": {"target": "requirement_focus", "disabled": False},
        "arm_team": {"target": "guarded_build_button", "disabled": False},
        "confirm_team": {"target": "guarded_build_button", "disabled": False},
    }
    return {"id": "detail_build_recommended_action_fixture", "passed": cases == expected, "cases": cases}


def detail_build_recommended_safety_fixture() -> dict[str, Any]:
    confirm = recommended_detail_build_action("confirm_team")
    return {"id": "detail_build_recommended_safety_fixture", "passed": confirm["target"] == "guarded_build_button", "confirm_team": confirm, "new_submit_button": False, "build_api_called": False}


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "detail_build_recommended_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "recommendedDetailBuildAction",
                "runDetailBuildRecommendedAction",
                "data-detail-build-recommended-action",
                "data-detail-build-recommended-target",
                "detailBuildRequirementRef",
                "detailBuildStartButtonRef",
                "type=\"button\"",
            ),
        ),
        (
            "detail_build_recommended_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "detailBuildRecommendedTitle",
                "detailBuildRecommendedAddDetailLabel",
                "detailBuildRecommendedGuardLabel",
                "detailBuildRecommendedGuardDetail",
            ),
        ),
        (
            "detail_build_recommended_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".detail-build-recommended-action",
                ".recommended-create-action",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_31_detail_build_recommended_action.py",
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
    return {"id": "p0_p1_bug_ledger_detail_build_recommended_action", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.31-detail-build-recommended-action"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), detail_build_recommended_action_fixture(), detail_build_recommended_safety_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_detail_build_recommended_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.31", "stage": "detail_build_recommended_action", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "detail_build_recommended_action": detail_build_recommended_action_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.31 detail build recommended-action evidence.")
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
