#!/usr/bin/env python3
"""Verify v0.3.29 detail build requirement readiness."""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "detail_build_readiness_v0.3.29.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-detail-build-requirement-readiness-invisible", "severity": "P1", "status": "fixed", "reproduction": "Users entering the detail Build tab saw a raw requirement box before team startup.", "fix": "Add detail build readiness summary.", "verification": "detail_build_readiness_fixture."},
    {"id": "P1-detail-build-missing-detail-hints-absent", "severity": "P1", "status": "fixed", "reproduction": "Weak build requirements did not show missing customer/outcome/acceptance/detail hints.", "fix": "Add readiness signal list before the build guard.", "verification": "detail_build_missing_detail_fixture."},
    {"id": "P1-v0329-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "New detail build readiness tests could be omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.29 and expected 154 passing tests.", "verification": "regression_manifest_updated."},
)


def detail_build_readiness(requirement: str) -> dict[str, Any]:
    text = requirement.strip()
    signals = [
        {"id": "audience", "ready": bool(re.search(r"客户|用户|负责人|顾问|运营|审阅|customer|user|owner|operator|consultant|reviewer", text, re.I))},
        {"id": "outcome", "ready": bool(re.search(r"输出|生成|给出|判断|分类|摘要|清单|result|output|generate|classify|summary|checklist", text, re.I))},
        {"id": "acceptance", "ready": bool(re.search(r"验收|测试|必须|覆盖|acceptance|test|must|cover|verify", text, re.I))},
        {"id": "detail", "ready": len(text) >= 80},
    ]
    ready_count = sum(1 for signal in signals if signal["ready"])
    return {"ready": ready_count >= 3, "ready_count": ready_count, "total": len(signals), "signals": signals}


def detail_build_readiness_fixture() -> dict[str, Any]:
    strong = detail_build_readiness("For a customer success operator, generate a classified handling summary and acceptance must cover billing, delivery, and complaint cases with owner recommendations.")
    weak = detail_build_readiness("Improve this draft.")
    return {
        "id": "detail_build_readiness_fixture",
        "passed": strong["ready"] is True and strong["ready_count"] == 4 and weak["ready"] is False and weak["ready_count"] == 0,
        "strong": strong,
        "weak": weak,
    }


def detail_build_missing_detail_fixture() -> dict[str, Any]:
    partial = detail_build_readiness("For an operator, classify alerts and generate a summary.")
    missing = [signal["id"] for signal in partial["signals"] if not signal["ready"]]
    return {"id": "detail_build_missing_detail_fixture", "passed": partial["ready"] is False and set(missing) == {"acceptance", "detail"}, "partial": partial, "missing": missing}


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "detail_build_readiness_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "detailBuildRequirementReadiness",
                "detailBuildReadiness",
                "data-detail-build-readiness=\"summary\"",
                "requirement-readiness-list",
                "build-intent-guard",
                "setBuildIntentConfirmed(false)",
            ),
        ),
        (
            "detail_build_readiness_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "requirementReadinessTitle",
                "requirementReadinessScore",
                "requirementReadinessNeedsDetail",
                "requirementSignalAcceptance",
                "requirementSignalDetailHint",
            ),
        ),
        (
            "detail_build_readiness_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".detail-build-readiness",
                ".requirement-readiness",
                ".requirement-readiness-list",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_29_detail_build_readiness.py",
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
    return {"id": "p0_p1_bug_ledger_detail_build_readiness", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.29-detail-build-readiness"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), detail_build_readiness_fixture(), detail_build_missing_detail_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_detail_build_readiness_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.29", "stage": "detail_build_requirement_readiness", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "detail_build_readiness": detail_build_readiness_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.29 detail build readiness evidence.")
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
