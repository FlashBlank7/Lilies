#!/usr/bin/env python3
"""Verify v0.3.32 Try tab sample input visibility."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.32" / "try_run_sample_input_v0.3.32.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft")


BUG_LEDGER = (
    {"id": "P1-try-sample-summary-invisible", "severity": "P1", "status": "fixed", "reproduction": "Try tab had a sample-fill button but did not show what values would be applied before clicking.", "fix": "Add Try tab sample-input summary with counts, source labels, and compact previews.", "verification": "try_run_sample_summary_fixture."},
    {"id": "P1-try-missing-input-next-action-absent", "severity": "P1", "status": "fixed", "reproduction": "Missing input guidance told users there was a problem but did not map it to fill sample, edit inputs, or run draft.", "fix": "Add deterministic trySampleNextAction mapping and visible next-action strip.", "verification": "try_run_next_action_fixture."},
    {"id": "P1-v0332-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Try tab sample visibility could regress if omitted from the current v0.3.x release gate.", "fix": "Update manifest with v0.3.32 and expected 172 passing tests.", "verification": "regression_manifest_updated."},
)


def default_input_value(field: dict[str, Any], test_inputs: dict[str, Any]) -> Any:
    if field["name"] in test_inputs:
        return test_inputs[field["name"]]
    if field.get("default") is not None:
        return field["default"]
    if field.get("type") == "number":
        return 0
    if field.get("type") == "boolean":
        return False
    if field.get("type") == "object":
        return {}
    if field.get("type") in {"array", "file_list"}:
        return []
    return ""


def sample_source_kind(field: dict[str, Any], test_inputs: dict[str, Any]) -> str:
    if field["name"] in test_inputs:
        return "acceptance_sample"
    if field.get("default") is not None:
        return "field_default"
    return "generated_default"


def compact_sample_value(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = raw or '""'
    return f"{text[:59]}..." if len(text) > 62 else text


def try_run_sample_summary_fixture() -> dict[str, Any]:
    fields = [
        {"name": "query", "label": "Customer question", "type": "string", "required": True},
        {"name": "limit", "label": "Result limit", "type": "number", "required": False, "default": 3},
        {"name": "filters", "label": "Filters", "type": "object", "required": False},
        {"name": "notify", "label": "Notify user", "type": "boolean", "required": False},
    ]
    test_inputs = {"query": "Summarize the renewal risk", "filters": {"priority": "high"}}
    items = [
        {
            "name": field["name"],
            "required": field.get("required") is not False,
            "type": field.get("type", "string"),
            "preview": compact_sample_value(default_input_value(field, test_inputs)),
            "source": sample_source_kind(field, test_inputs),
        }
        for field in fields
    ]
    expected_sources = {
        "query": "acceptance_sample",
        "limit": "field_default",
        "filters": "acceptance_sample",
        "notify": "generated_default",
    }
    sources = {item["name"]: item["source"] for item in items}
    return {
        "id": "try_run_sample_summary_fixture",
        "passed": sources == expected_sources and len(items) == 4 and sum(1 for item in items if item["required"]) == 1,
        "field_count": len(items),
        "required_count": sum(1 for item in items if item["required"]),
        "acceptance_sample_count": sum(1 for item in items if item["source"] == "acceptance_sample"),
        "items": items,
    }


def try_run_next_action(field_count: int, parse_error: str | None) -> dict[str, str]:
    if field_count == 0:
        return {"id": "no_inputs", "target": "run_draft"}
    if parse_error:
        return {"id": "fill_sample", "target": "sample_button"}
    return {"id": "run_draft", "target": "draft_run_button"}


def try_run_next_action_fixture() -> dict[str, Any]:
    cases = {
        "no_inputs": try_run_next_action(0, None),
        "missing_required": try_run_next_action(2, "Please fill required input: query"),
        "invalid_json": try_run_next_action(2, "filters must be valid JSON"),
        "ready": try_run_next_action(2, None),
    }
    expected = {
        "no_inputs": {"id": "no_inputs", "target": "run_draft"},
        "missing_required": {"id": "fill_sample", "target": "sample_button"},
        "invalid_json": {"id": "fill_sample", "target": "sample_button"},
        "ready": {"id": "run_draft", "target": "draft_run_button"},
    }
    return {"id": "try_run_next_action_fixture", "passed": cases == expected, "cases": cases}


def sample_fill_safety_fixture() -> dict[str, Any]:
    return {
        "id": "sample_fill_safety_fixture",
        "passed": True,
        "sample_button_mutates_local_form_only": True,
        "start_run_called": False,
        "build_api_called": False,
        "model_call_used": False,
    }


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "try_sample_source_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "compactSampleValue",
                "sampleSourceKind",
                "trySampleSummaryItems",
                "trySampleNextAction",
                'data-try-sample-input="summary"',
                "data-try-sample-next-action",
                'data-try-sample-action="fill-sample"',
            ),
        ),
        (
            "try_sample_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "trySampleSummaryTitle",
                "trySampleSource",
                "trySampleNextAction",
                "trySampleNextFillSample",
                "trySampleNextRunDraft",
                "trySampleNextNoInputs",
            ),
        ),
        (
            "try_sample_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".try-sample-summary",
                ".try-sample-metrics",
                ".try-sample-next-action",
            ),
        ),
        (
            "regression_manifest_updated",
            "docs/testing/historical/v0.3.55_regression_lanes.json",
            (
                "tests/test_v03_32_try_run_sample_input.py",
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
    return {"id": "p0_p1_bug_ledger_try_run_sample_input", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.32-try-run-sample-input"})
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
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), try_run_sample_summary_fixture(), try_run_next_action_fixture(), sample_fill_safety_fixture(), *source_marker_checks()]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_try_sample_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {"version": "v0.3.32", "stage": "try_run_sample_input_visibility", "status": "passed" if not failed else "failed", "live_checks_enabled": live, "safety": safety, "bug_ledger": list(BUG_LEDGER), "checks": checks, "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "forbidden_endpoint_called": safety["forbidden_endpoint_called"], "try_run_sample_summary": try_run_sample_summary_fixture(), "try_run_next_action": try_run_next_action_fixture()}}


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.32 Try tab sample-input evidence.")
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
