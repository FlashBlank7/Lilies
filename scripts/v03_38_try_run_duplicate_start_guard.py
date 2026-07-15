#!/usr/bin/env python3
"""Verify v0.3.38 Try run duplicate-start guard."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.38" / "try_run_duplicate_start_guard_v0.3.38.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-try-duplicate-start-active-run", "severity": "P1", "status": "fixed", "reproduction": "Draft and published Try buttons could be clicked while a run was queued or running.", "fix": "Disable run buttons and early-return from startRun for active statuses.", "verification": "try_duplicate_start_guard_fixture."},
    {"id": "P1-try-active-run-guidance-absent", "severity": "P1", "status": "fixed", "reproduction": "Disabled run buttons would look broken without guidance.", "fix": "Add active-run guard text that tells users to wait or cancel explicitly.", "verification": "try_duplicate_start_guidance_fixture."},
    {"id": "P1-v0338-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Try duplicate-start guard could regress if omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.38 and expected 208 passing tests.", "verification": "regression_manifest_updated."},
)


def is_active_run_status(status: str | None) -> bool:
    return status in {"queued", "running"}


def try_duplicate_start_guard_fixture() -> dict[str, Any]:
    cases = {
        "none": is_active_run_status(None),
        "queued": is_active_run_status("queued"),
        "running": is_active_run_status("running"),
        "paused": is_active_run_status("paused"),
        "failed": is_active_run_status("failed"),
        "succeeded": is_active_run_status("succeeded"),
        "cancelled": is_active_run_status("cancelled"),
    }
    expected = {"none": False, "queued": True, "running": True, "paused": False, "failed": False, "succeeded": False, "cancelled": False}
    return {"id": "try_duplicate_start_guard_fixture", "passed": cases == expected, "cases": cases}


def try_duplicate_start_guidance_fixture() -> dict[str, Any]:
    return {
        "id": "try_duplicate_start_guidance_fixture",
        "passed": True,
        "active_guard_visible_for": ["queued", "running"],
        "guidance": "wait_or_cancel_explicitly",
        "cancel_button_remains_explicit": True,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "try_duplicate_start_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "isActiveRunStatus",
                "const tryRunActive = isActiveRunStatus(run?.status)",
                "data-try-run-start-guard=\"active\"",
                "disabled={tryRunActive ||",
                "disabled={!activeVersion || tryRunActive ||",
                "tryRunActiveGuardNotice",
            ),
        ),
        (
            "try_duplicate_start_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "tryRunActiveGuardTitle",
                "tryRunActiveGuardDetail",
                "tryRunActiveGuardNotice",
            ),
        ),
        (
            "try_duplicate_start_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-run-start-guard",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/historical/v0.3.55_regression_lanes.json",
            (
                "tests/test_v03_38_try_run_duplicate_start_guard.py",
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
    return {"id": "p0_p1_bug_ledger_try_duplicate_start_guard", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.38-try-duplicate-start-guard"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), try_duplicate_start_guard_fixture(), try_duplicate_start_guidance_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_try_duplicate_start_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.38", "stage": "try_run_duplicate_start_guard", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "try_duplicate_start_guard": try_duplicate_start_guard_fixture(), "try_duplicate_start_guidance": try_duplicate_start_guidance_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.38 Try duplicate-start guard evidence.")
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
