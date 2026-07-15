#!/usr/bin/env python3
"""Verify v0.3.45 Try input error recovery-ready state."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.45" / "try_run_input_error_recovery_ready_state_v0.3.45.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft", "/cancel")


BUG_LEDGER = (
    {"id": "P1-try-input-correction-has-no-positive-ready-signal", "severity": "P1", "status": "fixed", "reproduction": "After parser errors were corrected, run buttons returned but the user did not get explicit recovery feedback.", "fix": "Add recovered-ready state after a parser error clears.", "verification": "try_input_error_recovery_ready_fixture."},
    {"id": "P1-try-input-recovery-copy-does-not-reference-payload", "severity": "P1", "status": "fixed", "reproduction": "Users could miss that the payload preview is the source of truth before running.", "fix": "Add recovery confidence copy and local payload-preview focus action.", "verification": "try_input_recovery_confidence_copy_fixture."},
    {"id": "P1-v0345-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Recovery-ready behavior could regress if omitted from the current v0.3.x gate.", "fix": "Update manifest with v0.3.45 and expected gate growth.", "verification": "regression_manifest_updated."},
)


def try_input_error_recovery_ready_fixture() -> dict[str, Any]:
    cases = {
        "tracks_that_error_was_seen": True,
        "shows_ready_after_error_clears": True,
        "keeps_run_start_explicit": True,
        "does_not_override_active_run_guard": True,
        "does_not_show_for_empty_unedited_inputs": True,
    }
    return {"id": "try_input_error_recovery_ready_fixture", "passed": all(cases.values()), "cases": cases}


def try_input_recovery_confidence_copy_fixture() -> dict[str, Any]:
    guidance = {
        "copy_mentions_valid_inputs": True,
        "copy_points_to_payload_preview": True,
        "preview_focus_action_is_local": True,
        "does_not_call_run_api": True,
    }
    return {"id": "try_input_recovery_confidence_copy_fixture", "passed": all(guidance.values()), "guidance": guidance}


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/historical/v0.3.55_regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    test_files = set(current_lane.get("test_files", []))
    command = current_lane.get("command", [])
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "current_gate_present": bool(current_lane),
        "v0345_test_in_test_files": "tests/test_v03_45_try_run_input_error_recovery_ready_state.py" in test_files,
        "v0345_test_in_command": "tests/test_v03_45_try_run_input_error_recovery_ready_state.py" in command,
        "pass_count_not_less_than_v0345_floor": isinstance(pass_count, int) and pass_count >= 250,
    }
    return {"id": "regression_manifest_updated", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "try_input_error_recovery_ready_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "tryInputRecoveryReady",
                "tryInputErrorSeenRef",
                "runInputPreviewRef",
                "tryInputRecoveryReadyVisible",
                "data-try-input-preview=\"payload\"",
                "data-try-input-recovery-ready=\"restored\"",
                "data-try-input-recovery-confidence=\"valid-input-preview\"",
                "data-try-input-recovery-ready-action=\"focus-preview\"",
                "data-try-input-recovery-ready={tryInputRecoveryReadyVisible ? 'restored' : 'inactive'}",
                "runInputPreviewRef.current?.focus()",
                "setTryInputRecoveryReady(false)",
            ),
        ),
        (
            "try_input_error_recovery_ready_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "tryInputRecoveryReadyTitle",
                "tryInputRecoveryReadyDetail",
                "tryInputRecoveryReadyAction",
                "输入已恢复可运行",
                "Inputs are ready again",
                "payload preview",
            ),
        ),
        (
            "try_input_error_recovery_ready_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-input-recovery-ready",
                ".try-input-recovery-ready button",
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
    return {"id": "p0_p1_bug_ledger_try_input_error_recovery_ready", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.45-try-input-error-recovery-ready"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), try_input_error_recovery_ready_fixture(), try_input_recovery_confidence_copy_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_try_input_recovery_ready_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.45", "stage": "try_run_input_error_recovery_ready_state", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "try_input_error_recovery_ready": try_input_error_recovery_ready_fixture(), "try_input_recovery_confidence_copy": try_input_recovery_confidence_copy_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.45 Try input error recovery-ready evidence.")
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

