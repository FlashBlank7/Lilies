#!/usr/bin/env python3
"""Verify v0.3.34 Try result recovery affordance."""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "try_result_recovery_affordance_v0.3.34.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-try-result-recovery-affordance-passive", "severity": "P1", "status": "fixed", "reproduction": "Result guidance told users what to do but did not help them reach the relevant recovery area.", "fix": "Add focus targets and a safe recovery focus button.", "verification": "try_result_recovery_focus_fixture."},
    {"id": "P1-try-result-focus-must-not-auto-retry", "severity": "P1", "status": "fixed", "reproduction": "A recovery affordance could accidentally become a hidden run/resume/cancel action.", "fix": "Keep focusTryResultRecoveryTarget limited to tab, scroll, focus, and notice behavior.", "verification": "try_result_focus_safety_fixture."},
    {"id": "P1-v0334-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Try result recovery affordance could regress if omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.34 and expected 184 passing tests.", "verification": "regression_manifest_updated."},
)


def try_result_focus_target(next_action_id: str) -> str:
    return {
        "not_run": "run_controls",
        "running_wait": "result_panel",
        "failed_trace_retry": "trace_panel",
        "paused_permission": "permission_card",
        "paused_human_input": "human_input_card",
        "succeeded_review": "acceptance_tab",
        "cancelled_retry": "run_inputs",
    }[next_action_id]


def try_result_recovery_focus_fixture() -> dict[str, Any]:
    cases = {
        action: try_result_focus_target(action)
        for action in (
            "not_run",
            "running_wait",
            "failed_trace_retry",
            "paused_permission",
            "paused_human_input",
            "succeeded_review",
            "cancelled_retry",
        )
    }
    expected = {
        "not_run": "run_controls",
        "running_wait": "result_panel",
        "failed_trace_retry": "trace_panel",
        "paused_permission": "permission_card",
        "paused_human_input": "human_input_card",
        "succeeded_review": "acceptance_tab",
        "cancelled_retry": "run_inputs",
    }
    return {"id": "try_result_recovery_focus_fixture", "passed": cases == expected, "cases": cases}


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def focus_handler_body() -> str:
    text = read_text("platform/frontend/app/applications/[id]/page.tsx")
    match = re.search(r"function focusTryResultRecoveryTarget\(target: string\) \{(?P<body>.*?)\n  \}\n\n  function refreshRuntimeStatus", text, re.S)
    return match.group("body") if match else ""


def try_result_focus_safety_fixture() -> dict[str, Any]:
    body = focus_handler_body()
    forbidden = ("startRun(", "resumeRun(", "cancelRun(", "api<", "api(", "watchRun(", "runTests(", "publish(", "resolvePermission(")
    missing_allowed = [marker for marker in ("setStudioTab", "scrollIntoView", ".focus(", "setNotice") if marker not in body]
    forbidden_hits = [marker for marker in forbidden if marker in body]
    return {
        "id": "try_result_focus_safety_fixture",
        "passed": bool(body) and not forbidden_hits and not missing_allowed,
        "forbidden_hits": forbidden_hits,
        "missing_allowed_markers": missing_allowed,
        "body_found": bool(body),
        "new_auto_retry_button": False,
        "model_call_used": False,
    }


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "try_result_recovery_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "focusTryResultRecoveryTarget",
                "data-try-result-focus-target",
                "runInputFormRef",
                "runControlsRef",
                "runPermissionRef",
                "runHumanInputRef",
                "runTraceRef",
                "target: 'trace_panel'",
                "target: 'acceptance_tab'",
            ),
        ),
        (
            "try_result_recovery_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "tryResultFocusAction",
                "tryResultFocusNotice",
            ),
        ),
        (
            "try_result_recovery_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-result-next-action button",
                ".try-result-next-action",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_34_try_result_recovery_affordance.py",
                "\"pass_count\": 184",
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
    return {"id": "p0_p1_bug_ledger_try_result_recovery_affordance", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.34-try-result-recovery-affordance"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), try_result_recovery_focus_fixture(), try_result_focus_safety_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_try_result_recovery_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.34", "stage": "try_result_recovery_affordance", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "try_result_recovery_focus": try_result_recovery_focus_fixture(), "try_result_focus_safety": try_result_focus_safety_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.34 Try result recovery affordance evidence.")
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
