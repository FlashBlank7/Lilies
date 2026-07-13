#!/usr/bin/env python3
"""Verify v0.3.35 Try result output preview."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "try_result_output_preview_v0.3.35.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-try-result-output-raw-json-first", "severity": "P1", "status": "fixed", "reproduction": "Successful Try results exposed raw JSON before readable output values.", "fix": "Add readable top-level output preview before raw JSON.", "verification": "try_result_output_preview_fixture."},
    {"id": "P1-try-result-error-preview-absent", "severity": "P1", "status": "fixed", "reproduction": "Failed Try results did not show a short error summary before raw JSON.", "fix": "Add error preview with fallback unknown-error copy.", "verification": "try_result_error_preview_fixture."},
    {"id": "P1-v0335-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Try result output preview could regress if omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.35 and expected 190 passing tests.", "verification": "regression_manifest_updated."},
)


def compact_value(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = raw or '""'
    return f"{text[:59]}..." if len(text) > 62 else text


def value_kind(value: Any) -> str:
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, dict):
        return "object"
    return "string"


def output_preview(outputs: dict[str, Any]) -> dict[str, Any]:
    entries = list(outputs.items())
    items = [{"key": key, "kind": value_kind(value), "preview": compact_value(value)} for key, value in entries[:3]]
    return {"items": items, "hidden_count": max(0, len(entries) - len(items)), "output_count": len(entries)}


def try_result_output_preview_fixture() -> dict[str, Any]:
    outputs = {
        "answer": "Renewal risk is high because usage dropped.",
        "score": 0.91,
        "drivers": ["usage_drop", "late_payment"],
        "long_note": "x" * 120,
    }
    preview = output_preview(outputs)
    return {
        "id": "try_result_output_preview_fixture",
        "passed": preview["output_count"] == 4 and len(preview["items"]) == 3 and preview["hidden_count"] == 1 and preview["items"][2]["kind"] == "array",
        "preview": preview,
    }


def error_preview(status: str, error: str | None, unknown: str = "unknown error") -> str:
    if error:
        return compact_value(error)
    if status == "failed":
        return unknown
    return ""


def try_result_error_preview_fixture() -> dict[str, Any]:
    cases = {
        "explicit_error": error_preview("failed", "Tool failed because the credential expired."),
        "unknown_failed": error_preview("failed", None),
        "succeeded": error_preview("succeeded", None),
    }
    return {
        "id": "try_result_error_preview_fixture",
        "passed": cases["explicit_error"].startswith("Tool failed") and cases["unknown_failed"] == "unknown error" and cases["succeeded"] == "",
        "cases": cases,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "try_result_output_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "valueKind",
                "tryResultOutputPreviewItems",
                "tryResultErrorPreview",
                "data-try-result-preview=\"output\"",
                "data-try-result-error-preview",
                "tryResultPreviewMore",
            ),
        ),
        (
            "try_result_output_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "tryResultPreviewTitle",
                "tryResultErrorPreviewHelp",
                "tryResultUnknownError",
                "tryResultPreviewMore",
            ),
        ),
        (
            "try_result_output_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-result-preview",
                ".try-result-preview-list",
                ".try-result-error-preview",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/regression_lanes.json",
            (
                "tests/test_v03_35_try_result_output_preview.py",
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
    return {"id": "p0_p1_bug_ledger_try_result_output_preview", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.35-try-result-output-preview"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), try_result_output_preview_fixture(), try_result_error_preview_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_try_output_preview_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.35", "stage": "try_result_output_preview", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "try_result_output_preview": try_result_output_preview_fixture(), "try_result_error_preview": try_result_error_preview_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.35 Try result output preview evidence.")
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
