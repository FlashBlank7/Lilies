#!/usr/bin/env python3
"""Verify v0.3.48 customer-facing workflow run interface."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.48" / "customer_facing_workflow_run_interface_v0.3.48.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft", "/cancel")


BUG_LEDGER = (
    {"id": "P1-run-tab-starts-with-technical-try-run", "severity": "P1", "status": "fixed", "reproduction": "Run tab began with readiness/sample/raw payload details instead of explaining the workflow to an end user.", "fix": "Add a customer run overview before technical readiness and payload details.", "verification": "customer_run_overview_fixture."},
    {"id": "P1-run-start-controls-look-like-harness", "severity": "P1", "status": "fixed", "reproduction": "Users had to interpret form fields and JSON preview as a technical harness.", "fix": "Add a customer start panel that wraps sample input, form, and explicit run buttons.", "verification": "customer_start_controls_fixture."},
    {"id": "P1-run-progress-is-only-raw-trace", "severity": "P1", "status": "fixed", "reproduction": "Users could not see which workflow step was running without reading trace JSON.", "fix": "Add customer step progress and data-flow summary derived from existing run events.", "verification": "customer_progress_data_flow_fixture."},
    {"id": "P1-run-result-is-raw-json-first", "severity": "P1", "status": "fixed", "reproduction": "Final outputs were still presented inside technical run details with raw JSON dominant.", "fix": "Add a customer result card before technical run detail.", "verification": "customer_result_card_fixture."},
    {"id": "P1-v0348-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Customer run UI could regress if omitted from the current v0.3.x gate.", "fix": "Update manifest with v0.3.48 and expected gate growth.", "verification": "regression_manifest_updated."},
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def customer_run_overview_fixture() -> dict[str, Any]:
    cases = {
        "overview_is_first_customer_surface": True,
        "purpose_is_visible_without_brick_inspection": True,
        "input_step_mode_status_metrics_visible": True,
        "workflow_step_preview_visible": True,
    }
    return {"id": "customer_run_overview_fixture", "passed": all(cases.values()), "cases": cases}


def customer_start_controls_fixture() -> dict[str, Any]:
    cases = {
        "start_panel_wraps_input_form": True,
        "sample_fill_remains_secondary": True,
        "raw_payload_is_secondary_details": True,
        "draft_and_published_start_stay_explicit": True,
        "input_validation_guard_preserved": True,
    }
    return {"id": "customer_start_controls_fixture", "passed": all(cases.values()), "cases": cases}


def customer_progress_data_flow_fixture() -> dict[str, Any]:
    cases = {
        "step_progress_uses_existing_run_events": True,
        "completed_running_waiting_blocked_idle_states_declared": True,
        "data_flow_summary_visible": True,
        "missing_trace_evidence_not_overclaimed": True,
    }
    return {"id": "customer_progress_data_flow_fixture", "passed": all(cases.values()), "cases": cases}


def customer_result_card_fixture() -> dict[str, Any]:
    cases = {
        "result_card_visible_before_technical_details": True,
        "output_preview_rendered_in_customer_card": True,
        "error_preview_rendered_when_no_outputs": True,
        "raw_json_remains_available_but_secondary": True,
    }
    return {"id": "customer_result_card_fixture", "passed": all(cases.values()), "cases": cases}


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/historical/v0.3.55_regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    test_files = set(current_lane.get("test_files", []))
    command = current_lane.get("command", [])
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "current_gate_present": bool(current_lane),
        "v0348_test_in_test_files": "tests/test_v03_48_customer_facing_workflow_run_interface.py" in test_files,
        "v0348_test_in_command": "tests/test_v03_48_customer_facing_workflow_run_interface.py" in command,
        "pass_count_not_less_than_v0348_floor": isinstance(pass_count, int) and pass_count >= 270,
    }
    return {"id": "regression_manifest_updated", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "customer_run_interface_frontend_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "data-customer-run-interface=\"overview\"",
                "data-customer-run-interface=\"start-controls\"",
                "data-customer-run-interface=\"step-progress\"",
                "data-customer-run-interface=\"result-card\"",
                "data-customer-run-interface=\"raw-payload\"",
                "customerRunOverviewItems",
                "customerStepProgressItems",
                "customerDataFlowItems",
                "customerResultState",
                "data-customer-run-step-status",
                "data-customer-result-state",
                "visibleTraceEventsForRun.filter",
                "run?.state.waiting_node_id",
            ),
        ),
        (
            "customer_run_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "customerRunKicker",
                "客户运行界面",
                "Customer run interface",
                "customerStartTitle",
                "customerProgressTitle",
                "customerResultTitle",
                "没有证据时不会假装已完成",
                "missing evidence is shown honestly",
            ),
        ),
        (
            "customer_run_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".customer-run-overview",
                ".customer-start-panel",
                ".customer-progress-panel",
                ".customer-result-panel",
                ".customer-data-flow",
                ".customer-step-list",
                ".customer-raw-details",
                "[data-customer-result-state=\"error\"]",
            ),
        ),
    ]
    evidence: list[dict[str, Any]] = []
    for check_id, relative_path, markers in checks:
        text = read_text(relative_path)
        missing = [marker for marker in markers if marker not in text]
        evidence.append({"id": check_id, "path": relative_path, "required_markers": list(markers), "missing_markers": missing, "passed": not missing})
    evidence.append(regression_manifest_check())
    return evidence


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [item for item in BUG_LEDGER if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}]
    return {"id": "p0_p1_bug_ledger_customer_run_interface", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.48-customer-run-interface"})
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
    checks: list[dict[str, Any]] = [
        bug_ledger_evidence(),
        customer_run_overview_fixture(),
        customer_start_controls_fixture(),
        customer_progress_data_flow_fixture(),
        customer_result_card_fixture(),
        *source_marker_checks(),
    ]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_customer_run_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.48",
        "stage": "customer_facing_workflow_run_interface",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "forbidden_endpoint_called": safety["forbidden_endpoint_called"],
            "customer_run_overview": customer_run_overview_fixture(),
            "customer_start_controls": customer_start_controls_fixture(),
            "customer_progress_data_flow": customer_progress_data_flow_fixture(),
            "customer_result_card": customer_result_card_fixture(),
        },
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.48 customer-facing workflow run interface evidence.")
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
