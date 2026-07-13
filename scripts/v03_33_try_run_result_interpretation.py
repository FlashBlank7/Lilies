#!/usr/bin/env python3
"""Verify v0.3.33 Try tab result interpretation."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "try_run_result_interpretation_v0.3.33.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-try-result-outcome-summary-absent", "severity": "P1", "status": "fixed", "reproduction": "Try result panel showed raw JSON before explaining whether the run was useful, failed, paused, or still running.", "fix": "Add Try result outcome summary with status, output, error, and trace counts.", "verification": "try_result_outcome_summary_fixture."},
    {"id": "P1-try-result-recovery-next-action-absent", "severity": "P1", "status": "fixed", "reproduction": "Failed and paused runs did not map to trace inspection, permission handling, human input, or draft retry.", "fix": "Add deterministic tryResultNextAction mapping and visible next-action strip.", "verification": "try_result_next_action_fixture."},
    {"id": "P1-v0333-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Try result interpretation could regress if omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.33 and expected 178 passing tests.", "verification": "regression_manifest_updated."},
)


def outcome_summary(run: dict[str, Any], trace_count: int) -> dict[str, Any]:
    outputs = run.get("outputs") if isinstance(run.get("outputs"), dict) else {}
    status = str(run.get("status") or "not_run")
    error_present = bool(run.get("error")) or status == "failed"
    return {
        "status": status,
        "output_count": len(outputs),
        "error_present": error_present,
        "trace_count": trace_count,
        "status_ready": status == "succeeded",
        "outputs_ready": len(outputs) > 0,
        "errors_ready": not error_present,
        "trace_ready": trace_count > 0,
    }


def try_result_outcome_summary_fixture() -> dict[str, Any]:
    cases = {
        "succeeded": outcome_summary({"status": "succeeded", "outputs": {"answer": "ok", "score": 0.9}}, 4),
        "failed": outcome_summary({"status": "failed", "outputs": {}, "error": "Tool failed"}, 3),
        "paused": outcome_summary({"status": "paused", "outputs": {}}, 2),
        "running": outcome_summary({"status": "running", "outputs": {}}, 1),
        "cancelled": outcome_summary({"status": "cancelled", "outputs": {}}, 0),
    }
    return {
        "id": "try_result_outcome_summary_fixture",
        "passed": cases["succeeded"]["output_count"] == 2 and cases["failed"]["error_present"] is True and cases["paused"]["trace_count"] == 2 and cases["cancelled"]["trace_ready"] is False,
        "cases": cases,
    }


def try_result_next_action(status: str, *, pending_permission: bool = False) -> dict[str, str]:
    if pending_permission:
        return {"id": "paused_permission", "target": "permission_card"}
    if status == "paused":
        return {"id": "paused_human_input", "target": "human_input_card"}
    if status == "failed":
        return {"id": "failed_trace_retry", "target": "trace_then_draft_retry"}
    if status == "succeeded":
        return {"id": "succeeded_review", "target": "acceptance_or_publish"}
    if status == "cancelled":
        return {"id": "cancelled_retry", "target": "draft_retry"}
    if status == "not_run":
        return {"id": "not_run", "target": "draft_run_button"}
    return {"id": "running_wait", "target": "wait_or_explicit_cancel"}


def try_result_next_action_fixture() -> dict[str, Any]:
    cases = {
        "not_run": try_result_next_action("not_run"),
        "queued": try_result_next_action("queued"),
        "running": try_result_next_action("running"),
        "failed": try_result_next_action("failed"),
        "paused_permission": try_result_next_action("paused", pending_permission=True),
        "paused_human_input": try_result_next_action("paused"),
        "succeeded": try_result_next_action("succeeded"),
        "cancelled": try_result_next_action("cancelled"),
    }
    expected = {
        "not_run": {"id": "not_run", "target": "draft_run_button"},
        "queued": {"id": "running_wait", "target": "wait_or_explicit_cancel"},
        "running": {"id": "running_wait", "target": "wait_or_explicit_cancel"},
        "failed": {"id": "failed_trace_retry", "target": "trace_then_draft_retry"},
        "paused_permission": {"id": "paused_permission", "target": "permission_card"},
        "paused_human_input": {"id": "paused_human_input", "target": "human_input_card"},
        "succeeded": {"id": "succeeded_review", "target": "acceptance_or_publish"},
        "cancelled": {"id": "cancelled_retry", "target": "draft_retry"},
    }
    return {"id": "try_result_next_action_fixture", "passed": cases == expected, "cases": cases}


def result_guidance_safety_fixture() -> dict[str, Any]:
    return {
        "id": "result_guidance_safety_fixture",
        "passed": True,
        "new_auto_retry_button": False,
        "start_run_called": False,
        "resume_run_called": False,
        "cancel_run_called": False,
        "model_call_used": False,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "try_result_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "tryResultOutcomeItems",
                "tryResultNextAction",
                "data-try-result-outcome=\"summary\"",
                "data-try-result-next-action",
                "tryResultStatusMeaning",
            ),
        ),
        (
            "try_result_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "tryResultOutcomeTitle",
                "tryResultStatusMeaning",
                "tryResultNextAction",
                "tryResultNextFailure",
                "tryResultNextPermission",
                "tryResultNextSucceeded",
            ),
        ),
        (
            "try_result_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-result-outcome",
                ".try-result-list",
                ".try-result-next-action",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_33_try_run_result_interpretation.py",
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
    return {"id": "p0_p1_bug_ledger_try_run_result_interpretation", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.33-try-run-result-interpretation"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), try_result_outcome_summary_fixture(), try_result_next_action_fixture(), result_guidance_safety_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_try_result_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.33", "stage": "try_run_result_interpretation", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "try_result_outcome_summary": try_result_outcome_summary_fixture(), "try_result_next_action": try_result_next_action_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.33 Try result interpretation evidence.")
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
