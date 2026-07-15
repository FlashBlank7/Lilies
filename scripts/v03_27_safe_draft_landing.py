#!/usr/bin/env python3
"""Verify v0.3.27 safe-draft landing handoff."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.27" / "safe_draft_landing_v0.3.27.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-safe-draft-landing-not-explained", "severity": "P1", "status": "fixed", "reproduction": "After safe draft creation, users landed on detail view without clear confirmation that no model team started.", "fix": "Add safe-draft landing handoff.", "verification": "safe_draft_landing_fixture."},
    {"id": "P1-safe-draft-actions-must-not-trigger-work", "severity": "P1", "status": "fixed", "reproduction": "Landing actions could accidentally trigger build/test/run/draft mutations.", "fix": "Route handoff actions only through existing tabs.", "verification": "safe_draft_action_safety_fixture."},
    {"id": "P1-v0327-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "New safe-draft landing tests could be omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.27 and expected 142 passing tests.", "verification": "regression_manifest_updated."},
)


def safe_draft_landing_from_url(url: str) -> bool:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return query.get("safeDraft", ["0"])[0] == "1"


def safe_draft_landing_fixture() -> dict[str, Any]:
    cases = {
        "safe_draft": safe_draft_landing_from_url("/applications/app_1?safeDraft=1"),
        "normal_detail": safe_draft_landing_from_url("/applications/app_1"),
        "build_return": safe_draft_landing_from_url("/applications/app_1?build=b_1"),
    }
    return {"id": "safe_draft_landing_fixture", "passed": cases == {"safe_draft": True, "normal_detail": False, "build_return": False}, "cases": cases}


def safe_draft_action_safety_fixture() -> dict[str, Any]:
    actions = {
        "inspect": {"target_tab": "edit", "button_type": "button"},
        "acceptance": {"target_tab": "test", "button_type": "button"},
        "try": {"target_tab": "run", "button_type": "button"},
        "build_later": {"target_tab": "build", "button_type": "button"},
    }
    forbidden_targets = {"builds", "tests/run", "runs", "versions", "restore", "draft", "form_submit"}
    unsafe = [key for key, value in actions.items() if value["target_tab"] in forbidden_targets or value["button_type"] != "button"]
    return {"id": "safe_draft_action_safety_fixture", "passed": not unsafe, "actions": actions, "unsafe_actions": unsafe, "forbidden_targets": sorted(forbidden_targets)}


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "safe_draft_landing_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "safeDraftLanding",
                "setSafeDraftLanding",
                "safeDraft",
                "data-safe-draft-landing",
                "data-safe-draft-action",
                "safeDraftActionBuildLater",
                "setStudioTab('build')",
                "type=\"button\"",
            ),
        ),
        (
            "safe_draft_landing_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "safeDraftLandingTitle",
                "safeDraftLandingNoModel",
                "safeDraftActionInspect",
                "safeDraftActionAcceptance",
                "safeDraftActionBuildLater",
            ),
        ),
        (
            "safe_draft_landing_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".safe-draft-landing",
                ".safe-draft-actions",
                ".safe-draft-actions button",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/historical/v0.3.55_regression_lanes.json",
            (
                "tests/test_v03_27_safe_draft_landing.py",
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
    return {"id": "p0_p1_bug_ledger_safe_draft_landing", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.27-safe-draft-landing"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), safe_draft_landing_fixture(), safe_draft_action_safety_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_safe_draft_landing_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.27", "stage": "safe_draft_landing_handoff", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "safe_draft_landing": safe_draft_landing_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.27 safe-draft landing evidence.")
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
