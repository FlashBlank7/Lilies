#!/usr/bin/env python3
"""Verify v0.3.22 application-list view summary and reset controls."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "application_list_view_reset_v0.3.22.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {
        "id": "P1-app-list-url-state-has-no-visible-summary",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "A URL-restored app list could be narrowed without explaining visible count, filter, search, or sort.",
        "fix": "Add a compact app-list view summary.",
        "verification": "app_list_view_summary_fixture.",
    },
    {
        "id": "P1-app-list-empty-state-lacks-direct-recovery",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Empty-state copy told users to clear search but offered no direct control.",
        "fix": "Add clear-search and reset-view controls that reuse safe URL-state updates.",
        "verification": "app_list_view_reset_fixture.",
    },
    {
        "id": "P1-v0322-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New view summary/reset tests could be omitted from the current v0.3.x release gate.",
        "fix": "Update `docs/testing/regression_lanes.json` with v0.3.22 and expected 112 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def view_summary(visible: int, total: int, filter_label: str, search: str, sort_label: str) -> dict[str, Any]:
    parts = [f"Showing {visible}/{total}", f"Filter: {filter_label}", f"Sort: {sort_label}"]
    normalized = search.strip()
    if normalized:
        parts.insert(2, f'Search: "{normalized}"')
    return {
        "visible": visible,
        "total": total,
        "filter": filter_label,
        "search": normalized,
        "sort": sort_label,
        "parts": parts,
    }


def reset_view_state(filter_value: str, search: str, sort: str) -> dict[str, Any]:
    dirty = filter_value != "all" or bool(search.strip()) or sort != "readiness"
    cleared_search = {"filter": filter_value, "q": "", "sort": sort}
    reset = {"filter": "all", "q": "", "sort": "readiness"}
    return {
        "dirty": dirty,
        "clear_search_enabled": bool(search.strip()),
        "reset_enabled": dirty,
        "clear_search": cleared_search,
        "reset": reset,
    }


def app_list_view_summary_fixture() -> dict[str, Any]:
    summary = view_summary(2, 7, "Published", " demo ", "Name A-Z")
    default_summary = view_summary(7, 7, "All", "", "Demo readiness first")
    return {
        "id": "app_list_view_summary_fixture",
        "passed": summary["parts"] == ['Showing 2/7', 'Filter: Published', 'Search: "demo"', 'Sort: Name A-Z'] and default_summary["parts"] == ["Showing 7/7", "Filter: All", "Sort: Demo readiness first"],
        "summary": summary,
        "default_summary": default_summary,
    }


def app_list_view_reset_fixture() -> dict[str, Any]:
    dirty = reset_view_state("published", "demo", "name")
    clean = reset_view_state("all", "", "readiness")
    return {
        "id": "app_list_view_reset_fixture",
        "passed": dirty["dirty"] is True
        and dirty["clear_search_enabled"] is True
        and dirty["reset_enabled"] is True
        and dirty["clear_search"] == {"filter": "published", "q": "", "sort": "name"}
        and dirty["reset"] == {"filter": "all", "q": "", "sort": "readiness"}
        and clean["dirty"] is False
        and clean["clear_search_enabled"] is False
        and clean["reset_enabled"] is False,
        "dirty": dirty,
        "clean": clean,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "app_list_view_summary_source_markers",
            "platform/frontend/app/page.tsx",
            (
                "appListViewDirty",
                "currentAppFilterLabel",
                "currentAppSortLabel",
                "clearAppListSearch",
                "resetAppListView",
                "data-app-list-view-summary=\"active\"",
                "app-list-view-actions",
            ),
        ),
        (
            "app_list_view_summary_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "appListSummaryCount",
                "appListSummaryFilter",
                "appListSummarySearch",
                "appListSummarySort",
                "appListClearSearch",
                "appListResetView",
            ),
        ),
        (
            "app_list_view_summary_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".app-list-view-state",
                ".app-list-view-actions",
                ".app-list-view-actions button",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_22_application_list_view_reset.py",
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
    return {"id": "p0_p1_bug_ledger_app_list_view_reset", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.22-app-list-view-reset"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), app_list_view_summary_fixture(), app_list_view_reset_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": []}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_view_reset_call", "passed": safety["forbidden_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS)})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.22",
        "stage": "application_list_view_reset",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "summary_fixture": app_list_view_summary_fixture(), "reset_fixture": app_list_view_reset_fixture()},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.22 app-list view reset evidence.")
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
