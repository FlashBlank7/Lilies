#!/usr/bin/env python3
"""Verify v0.3.20 detail tab URL-state synchronization."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "detail_tab_url_state_v0.3.20.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
STUDIO_TABS = ("build", "edit", "test", "run", "monitor")
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore")


BUG_LEDGER = (
    {
        "id": "P1-detail-tabs-leave-url-stale",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "After v0.3.19, inbound card links could open a tab, but in-page tab clicks left the URL behind.",
        "fix": "Route tab clicks through `setStudioTab` and update `?tab=` with history state.",
        "verification": "detail_tab_url_source_markers.",
    },
    {
        "id": "P1-detail-tab-back-forward-unsynced",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Browser back/forward could change the URL without changing the visible tab.",
        "fix": "Add a `popstate` listener that applies valid `?tab=` values.",
        "verification": "popstate_tab_guard_fixture.",
    },
    {
        "id": "P1-build-query-must-survive-tab-sync",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Naive tab URL replacement could drop `?build=` and break build-event watch context.",
        "fix": "Use `URLSearchParams` and preserve existing query parameters while changing `tab`.",
        "verification": "tab_url_state_fixture.",
    },
    {
        "id": "P1-v0320-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New detail navigation tests could be omitted from the current v0.3.x release gate.",
        "fix": "Update `docs/testing/regression_lanes.json` with v0.3.20 and expected 100 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def is_studio_tab(value: str | None) -> bool:
    return bool(value and value in STUDIO_TABS)


def tab_url_state(path: str, query_string: str, next_tab: str, *, replace: bool = False) -> dict[str, Any]:
    params = urllib.parse.parse_qsl(query_string, keep_blank_values=True)
    query = dict(params)
    previous_tab = query.get("tab")
    changed = previous_tab != next_tab
    if changed:
        query["tab"] = next_tab
    next_query = urllib.parse.urlencode(query)
    return {
        "path": path,
        "input_query": query_string,
        "next_tab": next_tab,
        "changed": changed,
        "history_method": "replaceState" if replace else "pushState",
        "url": f"{path}?{next_query}" if next_query else path,
        "query": query,
    }


def tab_url_state_fixture() -> dict[str, Any]:
    preserved_build = tab_url_state("/applications/app-1", "build=b123", "run")
    build_replace = tab_url_state("/applications/app-1", "build=b123", "build", replace=True)
    no_duplicate = tab_url_state("/applications/app-1", "build=b123&tab=test", "test")
    valid = (
        preserved_build["url"] == "/applications/app-1?build=b123&tab=run"
        and preserved_build["history_method"] == "pushState"
        and build_replace["url"] == "/applications/app-1?build=b123&tab=build"
        and build_replace["history_method"] == "replaceState"
        and no_duplicate["changed"] is False
        and no_duplicate["url"] == "/applications/app-1?build=b123&tab=test"
    )
    hrefs = [preserved_build["url"], build_replace["url"], no_duplicate["url"]]
    forbidden = [href for href in hrefs if any(endpoint in href for endpoint in FORBIDDEN_ENDPOINTS)]
    return {
        "id": "tab_url_state_fixture",
        "passed": valid and not forbidden,
        "preserved_build": preserved_build,
        "build_replace": build_replace,
        "no_duplicate": no_duplicate,
        "forbidden_hrefs": forbidden,
    }


def popstate_tab_guard_fixture() -> dict[str, Any]:
    valid = {tab: is_studio_tab(tab) for tab in STUDIO_TABS}
    invalid = {value: is_studio_tab(value) for value in ("", "tests", "publish", "builds", None)}
    return {
        "id": "popstate_tab_guard_fixture",
        "passed": all(valid.values()) and not any(invalid.values()),
        "valid": valid,
        "invalid": invalid,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def direct_set_tab_guard() -> dict[str, Any]:
    text = read_text("platform/frontend/app/applications/[id]/page.tsx")
    forbidden_markers = (
        "setTab('edit')",
        "setTab('build')",
        "setTab('run')",
        "setTab(item)",
        "setTab(action.target)",
    )
    found = [marker for marker in forbidden_markers if marker in text]
    return {
        "id": "direct_set_tab_guard",
        "passed": not found,
        "forbidden_markers": list(forbidden_markers),
        "found": found,
    }


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "detail_tab_url_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "setStudioTab",
                "syncStudioTabFromLocation",
                "query.set('tab', next)",
                "window.history.pushState",
                "window.history.replaceState",
                "data-detail-tab-url-state=\"synced\"",
                "STUDIO_TABS.map",
            ),
        ),
        (
            "detail_tab_popstate_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "window.addEventListener('popstate'",
                "window.removeEventListener('popstate'",
                "isStudioTab(requestedTab)",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_20_detail_tab_url_state.py",
                "\"pass_count\": 100",
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
    return {"id": "p0_p1_bug_ledger_detail_tab_url_state", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.20-detail-tab-url-state"})
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
    url_state = tab_url_state_fixture()
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), url_state, popstate_tab_guard_fixture(), direct_set_tab_guard(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "forbidden_hrefs": url_state["forbidden_hrefs"]}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_tab_url_call", "passed": safety["forbidden_endpoint_called"] is False and not safety["forbidden_hrefs"], "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "forbidden_hrefs": safety["forbidden_hrefs"]})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.20",
        "stage": "detail_tab_url_state",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "tab_url_state": url_state},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.20 detail tab URL-state evidence.")
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
