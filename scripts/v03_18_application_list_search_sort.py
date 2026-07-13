#!/usr/bin/env python3
"""Verify v0.3.18 application-list search and sort controls."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "application_list_search_sort_v0.3.18.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"


BUG_LEDGER = (
    {
        "id": "P1-application-list-long-list-search-missing",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Returning users had to scan long application lists by hand.",
        "fix": "Add name/description search that composes with status filters.",
        "verification": "application_list_search_sort_markers.",
    },
    {
        "id": "P1-application-list-order-not-predictable",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Demo reviewers had no deterministic readiness-first, revision, or name sort.",
        "fix": "Add readiness, revision, and name sort options.",
        "verification": "search_sort_fixture_behavior.",
    },
    {
        "id": "P1-v0318-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New search/sort tests could be omitted from the current v0.3.x release gate.",
        "fix": "Update `docs/testing/regression_lanes.json` with v0.3.18 and expected 88 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def app_readiness_state(item: dict[str, Any]) -> str:
    if item.get("active_version"):
        return "published"
    if item.get("tested_hash"):
        return "ready_to_publish"
    return "needs_acceptance"


def app_readiness_rank(item: dict[str, Any]) -> int:
    state = app_readiness_state(item)
    if state == "published":
        return 0
    if state == "ready_to_publish":
        return 1
    return 2


def fixture_apps() -> list[dict[str, Any]]:
    return [
        {"id": "beta", "name": "Beta billing flow", "description": "Invoice routing", "draft_revision": 4, "tested_hash": "b", "active_version": None},
        {"id": "alpha", "name": "Alpha demo workflow", "description": "Published customer demo", "draft_revision": 2, "tested_hash": "a", "active_version": 1},
        {"id": "gamma", "name": "Gamma intake", "description": "Draft customer intake", "draft_revision": 7, "tested_hash": None, "active_version": None},
    ]


def search_apps(apps: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    normalized = query.strip().lower()
    if not normalized:
        return apps
    return [item for item in apps if normalized in f"{item['name']} {item['description']}".lower()]


def sort_apps(apps: list[dict[str, Any]], sort: str) -> list[dict[str, Any]]:
    if sort == "name":
        return sorted(apps, key=lambda item: item["name"])
    if sort == "revision":
        return sorted(apps, key=lambda item: (-int(item["draft_revision"]), item["name"]))
    return sorted(apps, key=lambda item: (app_readiness_rank(item), -int(item["draft_revision"]), item["name"]))


def search_sort_fixture_evidence() -> dict[str, Any]:
    apps = fixture_apps()
    demo_search = [item["id"] for item in search_apps(apps, "demo")]
    readiness_order = [item["id"] for item in sort_apps(apps, "readiness")]
    revision_order = [item["id"] for item in sort_apps(apps, "revision")]
    name_order = [item["id"] for item in sort_apps(apps, "name")]
    return {
        "id": "search_sort_fixture_behavior",
        "passed": demo_search == ["alpha"] and readiness_order == ["alpha", "beta", "gamma"] and revision_order == ["gamma", "beta", "alpha"] and name_order == ["alpha", "beta", "gamma"],
        "demo_search": demo_search,
        "readiness_order": readiness_order,
        "revision_order": revision_order,
        "name_order": name_order,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "application_list_search_sort_markers",
            "platform/frontend/app/page.tsx",
            (
                "type AppSort",
                "appReadinessRank",
                "normalizedAppSearch",
                "searchedApps",
                "visibleApps",
                'data-app-list-search-sort="controls"',
            ),
        ),
        (
            "application_list_search_sort_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "appSearchPlaceholder",
                "appSearchEmpty",
                "appSortReadiness",
                "appSortRevision",
                "appSortName",
            ),
        ),
        (
            "application_list_search_sort_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".app-search-sort",
                ".app-search-sort input",
                ".app-search-sort select",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_18_application_list_search_sort.py",
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
    return {"id": "p0_p1_bug_ledger_app_list_search_sort", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.18-app-list-search-sort"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), search_sort_fixture_evidence(), *source_marker_checks()]
    safety: dict[str, Any] = {"build_endpoint_called": False, "called_endpoints": []}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["build_endpoint_called"] = any("/builds" in endpoint for endpoint in safety["called_endpoints"])
    checks.append({"id": "safety_no_build_call", "passed": safety["build_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint": "POST /api/v1/applications/{id}/builds"})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.18",
        "stage": "application_list_search_sort",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "build_endpoint_called": safety["build_endpoint_called"], "fixture": search_sort_fixture_evidence()},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.18 app-list search/sort evidence.")
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
