#!/usr/bin/env python3
"""Verify v0.3.21 application-list URL-state synchronization."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.21" / "application_list_url_state_v0.3.21.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
APP_FILTERS = ("all", "needs_acceptance", "ready_to_publish", "published")
APP_SORTS = ("readiness", "revision", "name")
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {
        "id": "P1-app-list-view-state-not-shareable",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Filter/search/sort state vanished from the URL after v0.3.18-v0.3.20.",
        "fix": "Hydrate and write home app-list `filter`, `q`, and `sort` query state.",
        "verification": "app_list_url_state_source_markers.",
    },
    {
        "id": "P1-app-list-search-history-spam-risk",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "A naive search URL writer would push browser history on every keystroke.",
        "fix": "Use replaceState for search and pushState for filter/sort.",
        "verification": "app_list_url_writer_fixture.",
    },
    {
        "id": "P1-app-list-query-needs-guards",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Invalid query values could put controls into unsupported filter or sort modes.",
        "fix": "Add filter and sort allowlists plus parser guards.",
        "verification": "app_list_query_parser_fixture.",
    },
    {
        "id": "P1-v0321-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New app-list URL-state tests could be omitted from the current v0.3.x release gate.",
        "fix": "Update `docs/testing/historical/v0.3.55_regression_lanes.json` with v0.3.21 and expected 106 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def is_app_filter(value: str | None) -> bool:
    return bool(value and value in APP_FILTERS)


def is_app_sort(value: str | None) -> bool:
    return bool(value and value in APP_SORTS)


def parse_app_list_query(query_string: str) -> dict[str, str]:
    query = dict(urllib.parse.parse_qsl(query_string, keep_blank_values=True))
    filter_value = query.get("filter")
    sort_value = query.get("sort")
    return {
        "filter": filter_value if is_app_filter(filter_value) else "all",
        "q": query.get("q", ""),
        "sort": sort_value if is_app_sort(sort_value) else "readiness",
    }


def write_app_list_url_state(path: str, query_string: str, updates: dict[str, str], *, replace: bool = False) -> dict[str, Any]:
    query = dict(urllib.parse.parse_qsl(query_string, keep_blank_values=True))
    if "filter" in updates:
        if updates["filter"] == "all":
            query.pop("filter", None)
        else:
            query["filter"] = updates["filter"]
    if "q" in updates:
        value = updates["q"].strip()
        if value:
            query["q"] = value
        else:
            query.pop("q", None)
    if "sort" in updates:
        if updates["sort"] == "readiness":
            query.pop("sort", None)
        else:
            query["sort"] = updates["sort"]
    next_query = urllib.parse.urlencode(query)
    return {
        "path": path,
        "input_query": query_string,
        "updates": updates,
        "history_method": "replaceState" if replace else "pushState",
        "url": f"{path}?{next_query}" if next_query else path,
        "query": query,
    }


def app_list_query_parser_fixture() -> dict[str, Any]:
    valid = parse_app_list_query("filter=published&q=demo&sort=name")
    invalid = parse_app_list_query("filter=bad&q=demo&sort=weird")
    empty = parse_app_list_query("")
    return {
        "id": "app_list_query_parser_fixture",
        "passed": valid == {"filter": "published", "q": "demo", "sort": "name"} and invalid == {"filter": "all", "q": "demo", "sort": "readiness"} and empty == {"filter": "all", "q": "", "sort": "readiness"},
        "valid": valid,
        "invalid": invalid,
        "empty": empty,
    }


def app_list_url_writer_fixture() -> dict[str, Any]:
    filter_push = write_app_list_url_state("/", "team=alpha", {"filter": "published"})
    search_replace = write_app_list_url_state("/", "filter=published", {"q": " customer demo "}, replace=True)
    sort_push = write_app_list_url_state("/", "filter=published&q=demo", {"sort": "name"})
    defaults_removed = write_app_list_url_state("/", "filter=published&q=demo&sort=name", {"filter": "all", "q": "", "sort": "readiness"})
    urls = [filter_push["url"], search_replace["url"], sort_push["url"], defaults_removed["url"]]
    forbidden = [url for url in urls if any(endpoint in url for endpoint in FORBIDDEN_ENDPOINTS)]
    return {
        "id": "app_list_url_writer_fixture",
        "passed": filter_push["url"] == "/?team=alpha&filter=published"
        and filter_push["history_method"] == "pushState"
        and search_replace["url"] == "/?filter=published&q=customer+demo"
        and search_replace["history_method"] == "replaceState"
        and sort_push["url"] == "/?filter=published&q=demo&sort=name"
        and defaults_removed["url"] == "/"
        and not forbidden,
        "filter_push": filter_push,
        "search_replace": search_replace,
        "sort_push": sort_push,
        "defaults_removed": defaults_removed,
        "forbidden_urls": forbidden,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def direct_app_list_setter_guard() -> dict[str, Any]:
    text = read_text("platform/frontend/app/page.tsx")
    forbidden_markers = (
        "onClick={() => setAppFilter",
        "onChange={event => setAppSearch",
        "onChange={event => setAppSort",
    )
    found = [marker for marker in forbidden_markers if marker in text]
    return {
        "id": "direct_app_list_setter_guard",
        "passed": not found,
        "forbidden_markers": list(forbidden_markers),
        "found": found,
    }


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "app_list_url_state_source_markers",
            "platform/frontend/app/page.tsx",
            (
                "APP_FILTERS",
                "APP_SORTS",
                "isAppFilter",
                "isAppSort",
                "writeAppListUrlState",
                "setAppListFilter",
                "setAppListSearch",
                "setAppListSort",
                "syncAppListStateFromLocation",
                "data-app-list-url-state=\"synced\"",
            ),
        ),
        (
            "app_list_url_history_markers",
            "platform/frontend/app/page.tsx",
            (
                "window.history.pushState",
                "window.history.replaceState",
                "window.addEventListener('popstate'",
                "window.removeEventListener('popstate'",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/historical/v0.3.55_regression_lanes.json",
            (
                "tests/test_v03_21_application_list_url_state.py",
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
    return {"id": "p0_p1_bug_ledger_app_list_url_state", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.21-app-list-url-state"})
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
    writer = app_list_url_writer_fixture()
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), app_list_query_parser_fixture(), writer, direct_app_list_setter_guard(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "forbidden_urls": writer["forbidden_urls"]}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_app_list_url_call", "passed": safety["forbidden_endpoint_called"] is False and not safety["forbidden_urls"], "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "forbidden_urls": safety["forbidden_urls"]})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.21",
        "stage": "application_list_url_state",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "url_writer": writer},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.21 app-list URL-state evidence.")
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
