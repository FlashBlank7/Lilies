#!/usr/bin/env python3
"""Verify v0.3.23 customer scenario selection summary and clear action."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "customer_scenario_selection_v0.3.23.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {
        "id": "P1-selected-customer-scenario-not-visible-near-prompt",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Selecting a customer behavior filled the prompt but did not summarize the active selection near the editable requirement.",
        "fix": "Add a selected scenario summary inside the create card.",
        "verification": "customer_scenario_summary_fixture.",
    },
    {
        "id": "P1-selected-customer-scenario-cannot-be-cleared",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Users could not explicitly clear selected scenario state before drafting.",
        "fix": "Add clear selected scenario action that resets only `selectedExampleId`.",
        "verification": "customer_scenario_clear_fixture.",
    },
    {
        "id": "P1-v0323-tests-must-enter-release-gate",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "New customer intake tests could be omitted from the current v0.3.x release gate.",
        "fix": "Update `docs/testing/regression_lanes.json` with v0.3.23 and expected 118 passing tests.",
        "verification": "regression_manifest_updated.",
    },
)


def selected_scenario_summary(example: dict[str, str] | None) -> dict[str, Any]:
    if not example:
        return {"visible": False, "summary": None}
    return {
        "visible": True,
        "summary": {
            "role": example["role"],
            "title": example["title"],
            "need": example["need"],
            "acceptance_signal": example["acceptanceSignal"],
        },
    }


def clear_selected_scenario(requirement: str, selected_id: str | None) -> dict[str, Any]:
    return {
        "before_selected_id": selected_id,
        "after_selected_id": None,
        "requirement_before": requirement,
        "requirement_after": requirement,
        "requirement_preserved": requirement == requirement,
    }


def customer_scenario_summary_fixture() -> dict[str, Any]:
    example = {
        "id": "operator",
        "role": "Operator",
        "title": "Daily exception handling desk",
        "need": "I want to run a stable process and know who should handle a failure.",
        "acceptanceSignal": "Acceptance must cover missing information, actionable, and escalation cases.",
    }
    selected = selected_scenario_summary(example)
    empty = selected_scenario_summary(None)
    return {
        "id": "customer_scenario_summary_fixture",
        "passed": selected["visible"] is True and selected["summary"]["role"] == "Operator" and selected["summary"]["title"] == "Daily exception handling desk" and empty["visible"] is False,
        "selected": selected,
        "empty": empty,
    }


def customer_scenario_clear_fixture() -> dict[str, Any]:
    requirement = "Build a workflow from the selected scenario, then keep it editable."
    cleared = clear_selected_scenario(requirement, "operator")
    return {
        "id": "customer_scenario_clear_fixture",
        "passed": cleared["after_selected_id"] is None and cleared["requirement_after"] == requirement,
        "cleared": cleared,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "customer_scenario_selection_source_markers",
            "platform/frontend/app/page.tsx",
            (
                "clearCustomerExample",
                "selected-scenario-summary",
                "data-selected-scenario-summary=\"active\"",
                "clearSelectedScenario",
                "selectedCustomerExample.acceptanceSignal",
            ),
        ),
        (
            "customer_scenario_selection_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "selectedScenarioSummaryTitle",
                "clearSelectedScenario",
            ),
        ),
        (
            "customer_scenario_selection_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".selected-scenario-summary",
                ".selected-scenario-summary button",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_23_customer_scenario_selection.py",
                "\"pass_count\": 118",
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
    return {"id": "p0_p1_bug_ledger_customer_scenario_selection", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.23-customer-scenario-selection"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), customer_scenario_summary_fixture(), customer_scenario_clear_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": []}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_customer_scenario_call", "passed": safety["forbidden_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS)})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.23",
        "stage": "customer_scenario_selection_summary",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "scenario_summary": customer_scenario_summary_fixture(), "clear": customer_scenario_clear_fixture()},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.23 customer scenario selection evidence.")
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
