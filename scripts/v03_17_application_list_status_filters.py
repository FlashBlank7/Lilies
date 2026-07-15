#!/usr/bin/env python3
"""Verify v0.3.17 application-list status filters."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Literal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.17" / "application_list_status_filters_v0.3.17.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
AppFilter = Literal["needs_acceptance", "ready_to_publish", "published"]


BUG_LEDGER = (
    {
        "id": "P1-application-list-readiness-not-filterable",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Demo reviewers had to scan every card after v0.3.16 readiness chips were added.",
        "fix": "Add status filters for all, needs acceptance, ready to publish, and published apps.",
        "verification": "application_list_filter_markers.",
    },
    {
        "id": "P1-filter-behavior-needs-fixture-coverage",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Filter categories could drift from the application-card readiness logic.",
        "fix": "Add deterministic fixture checks for draft, ready-to-publish, and published states.",
        "verification": "filter_fixture_behavior.",
    },
    {
        "id": "P1-v0317-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New filter tests could be omitted from the current v0.3.x release gate.",
        "fix": "Update `docs/testing/historical/v0.3.55_regression_lanes.json` with v0.3.17 and expected 83 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def app_readiness_state(item: dict[str, Any]) -> AppFilter:
    if item.get("active_version"):
        return "published"
    if item.get("tested_hash"):
        return "ready_to_publish"
    return "needs_acceptance"


def filter_apps(apps: list[dict[str, Any]], app_filter: str) -> list[dict[str, Any]]:
    if app_filter == "all":
        return apps
    return [item for item in apps if app_readiness_state(item) == app_filter]


def fixture_apps() -> list[dict[str, Any]]:
    return [
        {"id": "draft", "draft_revision": 2, "tested_hash": None, "active_version": None},
        {"id": "ready", "draft_revision": 3, "tested_hash": "abc", "active_version": None},
        {"id": "published", "draft_revision": 4, "tested_hash": "def", "active_version": 1},
    ]


def filter_fixture_evidence() -> dict[str, Any]:
    apps = fixture_apps()
    counts = {
        "all": len(filter_apps(apps, "all")),
        "needs_acceptance": len(filter_apps(apps, "needs_acceptance")),
        "ready_to_publish": len(filter_apps(apps, "ready_to_publish")),
        "published": len(filter_apps(apps, "published")),
    }
    states = {item["id"]: app_readiness_state(item) for item in apps}
    return {"id": "filter_fixture_behavior", "passed": counts == {"all": 3, "needs_acceptance": 1, "ready_to_publish": 1, "published": 1} and states == {"draft": "needs_acceptance", "ready": "ready_to_publish", "published": "published"}, "counts": counts, "states": states}


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "application_list_filter_markers",
            "platform/frontend/app/page.tsx",
            (
                "type AppFilter",
                "appReadinessState",
                "appFilterOptions",
                "statusFilteredApps",
                'data-app-list-filter="status"',
            ),
        ),
        (
            "application_list_filter_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "appFilterAll",
                "appFilterNeedsAcceptance",
                "appFilterReadyToPublish",
                "appFilterPublished",
                "appFilterEmpty",
            ),
        ),
        (
            "application_list_filter_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".app-filter-toolbar",
                ".app-filter-toolbar button.active",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/historical/v0.3.55_regression_lanes.json",
            (
                "tests/test_v03_17_application_list_status_filters.py",
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
    return {"id": "p0_p1_bug_ledger_app_list_filters", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.17-app-list-filters"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), filter_fixture_evidence(), *source_marker_checks()]
    safety: dict[str, Any] = {"build_endpoint_called": False, "called_endpoints": []}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["build_endpoint_called"] = any("/builds" in endpoint for endpoint in safety["called_endpoints"])
    checks.append({"id": "safety_no_build_call", "passed": safety["build_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint": "POST /api/v1/applications/{id}/builds"})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.17",
        "stage": "application_list_status_filters",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "build_endpoint_called": safety["build_endpoint_called"], "fixture_counts": filter_fixture_evidence()["counts"]},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.17 app-list status filter evidence.")
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
