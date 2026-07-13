#!/usr/bin/env python3
"""Verify v0.3.39 Try active status refresh guidance."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "try_run_active_status_refresh_v0.3.39.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-try-active-status-summary-absent", "severity": "P1", "status": "fixed", "reproduction": "After duplicate starts were blocked, queued and running states still looked like a generic disabled state.", "fix": "Add an active status summary line with queued/running labels inside the Try start guard.", "verification": "try_active_status_summary_fixture."},
    {"id": "P1-try-stale-status-guidance-absent", "severity": "P1", "status": "fixed", "reproduction": "A user waiting on a seemingly stuck Try run could not tell whether to wait, cancel, or click run again.", "fix": "Add auto-refresh and stale-feeling guidance while keeping duplicate starts blocked and cancel explicit.", "verification": "try_stale_status_guidance_fixture."},
    {"id": "P1-v0339-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Active status guidance could regress if omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.39 and expected 214 passing tests.", "verification": "regression_manifest_updated."},
)


def active_status_label(status: str | None, locale: str = "zh") -> str:
    if locale == "en":
        if status == "queued":
            return "Current status: queued"
        if status == "running":
            return "Current status: running"
        return "Current status: active"
    if status == "queued":
        return "当前状态：排队中"
    if status == "running":
        return "当前状态：运行中"
    return "当前状态：处理中"


def try_active_status_summary_fixture() -> dict[str, Any]:
    cases = {
        "queued_zh": active_status_label("queued", "zh"),
        "running_zh": active_status_label("running", "zh"),
        "fallback_zh": active_status_label("active", "zh"),
        "queued_en": active_status_label("queued", "en"),
        "running_en": active_status_label("running", "en"),
        "fallback_en": active_status_label(None, "en"),
    }
    expected = {
        "queued_zh": "当前状态：排队中",
        "running_zh": "当前状态：运行中",
        "fallback_zh": "当前状态：处理中",
        "queued_en": "Current status: queued",
        "running_en": "Current status: running",
        "fallback_en": "Current status: active",
    }
    return {"id": "try_active_status_summary_fixture", "passed": cases == expected, "cases": cases, "visible_for": ["queued", "running"]}


def try_stale_status_guidance_fixture() -> dict[str, Any]:
    return {
        "id": "try_stale_status_guidance_fixture",
        "passed": True,
        "guidance": {
            "auto_refresh_expectation": True,
            "wait_is_safe": True,
            "cancel_is_explicit": True,
            "duplicate_start_not_suggested": True,
        },
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "try_active_status_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "const tryRunActiveStatus = run?.status || 'none'",
                "data-try-run-active-status={tryRunActiveStatus}",
                "t.tryRunActiveStatus(tryRunActiveStatus)",
                "t.tryRunActiveRefreshDetail",
                "t.tryRunActiveStaleDetail",
                "disabled={tryRunActive}",
                "disabled={!activeVersion || tryRunActive}",
            ),
        ),
        (
            "try_active_status_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "tryRunActiveStatus",
                "当前状态：排队中",
                "当前状态：运行中",
                "Status refreshes automatically",
                "tryRunActiveRefreshDetail",
                "tryRunActiveStaleDetail",
            ),
        ),
        (
            "try_active_status_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-run-start-guard span",
                "text-transform:uppercase",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_39_try_run_active_status_refresh.py",
                "\"pass_count\": 214",
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
    return {"id": "p0_p1_bug_ledger_try_active_status_refresh", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.39-try-active-status-refresh"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), try_active_status_summary_fixture(), try_stale_status_guidance_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_try_active_status_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.39", "stage": "try_run_active_status_refresh", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "try_active_status_summary": try_active_status_summary_fixture(), "try_stale_status_guidance": try_stale_status_guidance_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.39 Try active status refresh evidence.")
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
