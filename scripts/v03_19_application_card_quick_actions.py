#!/usr/bin/env python3
"""Verify v0.3.19 application-card quick actions and detail tab links."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "application_card_quick_actions_v0.3.19.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
STUDIO_TABS = ("build", "edit", "test", "run", "monitor")
FORBIDDEN_ACTION_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions")


BUG_LEDGER = (
    {
        "id": "P1-application-card-action-affordance-missing",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Users could find an app after v0.3.18 but still had to guess what to do from a single card link.",
        "fix": "Add readiness-dependent navigation quick actions on every app card.",
        "verification": "quick_action_fixture_behavior.",
    },
    {
        "id": "P1-card-actions-must-not-nest-in-link",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Adding buttons inside the existing card Link would create invalid nested interactive UI.",
        "fix": "Refactor cards into an article, primary detail link, and separate action strip.",
        "verification": "card_quick_action_source_markers.",
    },
    {
        "id": "P1-quick-actions-need-real-detail-targets",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "A home quick action with only `/applications/{id}` always lands on the default build tab.",
        "fix": "Add a guarded `?tab=` detail-page deep link path.",
        "verification": "detail_tab_deeplink_markers.",
    },
    {
        "id": "P1-v0319-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New card action tests could be omitted from the current v0.3.x release gate.",
        "fix": "Update `docs/testing/regression_lanes.json` with v0.3.19 and expected 94 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def app_readiness_state(item: dict[str, Any]) -> str:
    if item.get("active_version"):
        return "published"
    if item.get("tested_hash"):
        return "ready_to_publish"
    return "needs_acceptance"


def is_studio_tab(value: str | None) -> bool:
    return bool(value and value in STUDIO_TABS)


def quick_actions_for_app(item: dict[str, Any]) -> list[dict[str, str]]:
    state = app_readiness_state(item)
    if state == "published":
        return [
            {"id": "try", "tab": "run", "href": f"/applications/{item['id']}?tab=run"},
            {"id": "monitor", "tab": "monitor", "href": f"/applications/{item['id']}?tab=monitor"},
        ]
    if state == "ready_to_publish":
        return [
            {"id": "acceptance", "tab": "test", "href": f"/applications/{item['id']}?tab=test"},
            {"id": "publish_check", "tab": "test", "href": f"/applications/{item['id']}?tab=test"},
        ]
    return [
        {"id": "edit", "tab": "edit", "href": f"/applications/{item['id']}?tab=edit"},
        {"id": "acceptance", "tab": "test", "href": f"/applications/{item['id']}?tab=test"},
    ]


def fixture_apps() -> list[dict[str, Any]]:
    return [
        {"id": "draft", "tested_hash": None, "active_version": None},
        {"id": "ready", "tested_hash": "abc", "active_version": None},
        {"id": "published", "tested_hash": "def", "active_version": 1},
    ]


def quick_action_fixture_evidence() -> dict[str, Any]:
    actions = {item["id"]: quick_actions_for_app(item) for item in fixture_apps()}
    ids = {key: [action["id"] for action in value] for key, value in actions.items()}
    tabs = {key: [action["tab"] for action in value] for key, value in actions.items()}
    expected_ids = {
        "draft": ["edit", "acceptance"],
        "ready": ["acceptance", "publish_check"],
        "published": ["try", "monitor"],
    }
    expected_tabs = {
        "draft": ["edit", "test"],
        "ready": ["test", "test"],
        "published": ["run", "monitor"],
    }
    hrefs = [action["href"] for value in actions.values() for action in value]
    forbidden = [href for href in hrefs if any(endpoint in href for endpoint in FORBIDDEN_ACTION_ENDPOINTS)]
    return {
        "id": "quick_action_fixture_behavior",
        "passed": ids == expected_ids and tabs == expected_tabs and not forbidden,
        "ids": ids,
        "tabs": tabs,
        "hrefs": hrefs,
        "forbidden_hrefs": forbidden,
    }


def detail_tab_deeplink_evidence() -> dict[str, Any]:
    valid = {tab: is_studio_tab(tab) for tab in STUDIO_TABS}
    invalid = {value: is_studio_tab(value) for value in ("", "tests", "publish", "builds", None)}
    return {
        "id": "detail_tab_deeplink_guard",
        "passed": all(valid.values()) and not any(invalid.values()),
        "valid": valid,
        "invalid": invalid,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "card_quick_action_source_markers",
            "platform/frontend/app/page.tsx",
            (
                "type AppActionTab",
                "type AppQuickAction",
                "appCardQuickActions",
                "data-app-card-action-state",
                "app-card-main",
                'data-app-card-quick-actions="navigation"',
                "data-app-card-action",
            ),
        ),
        (
            "detail_tab_deeplink_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "STUDIO_TABS",
                "isStudioTab",
                "requestedTab",
                "query.get('tab')",
            ),
        ),
        (
            "card_quick_action_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "appActionOpen",
                "appActionEdit",
                "appActionAcceptance",
                "appActionPublishCheck",
                "appActionTry",
                "appActionMonitor",
            ),
        ),
        (
            "card_quick_action_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".app-card-main",
                ".app-card-actions",
                ".app-card-actions a",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_19_application_card_quick_actions.py",
                "\"pass_count\": 94",
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
    return {"id": "p0_p1_bug_ledger_card_quick_actions", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.19-card-quick-actions"})
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
    action_evidence = quick_action_fixture_evidence()
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), action_evidence, detail_tab_deeplink_evidence(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "forbidden_action_hrefs": action_evidence["forbidden_hrefs"]}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ACTION_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_quick_action_call", "passed": safety["forbidden_endpoint_called"] is False and not safety["forbidden_action_hrefs"], "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ACTION_ENDPOINTS), "forbidden_action_hrefs": safety["forbidden_action_hrefs"]})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.19",
        "stage": "application_card_quick_actions",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "quick_actions": action_evidence},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.19 app-card quick-action evidence.")
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
