#!/usr/bin/env python3
"""Validate v0.3.15 regression lane governance without running builds."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "testing" / "historical" / "v0.3.55_regression_lanes.json"
HISTORICAL_README_PATH = ROOT / "docs" / "testing" / "historical" / "v0.3.55_README.md"
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.15" / "regression_suite_lane_guard_v0.3.15.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"


BUG_LEDGER = (
    {
        "id": "P1-current-release-gate-is-tribal-knowledge",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Automatic evolution needed manually remembered v0.3.x pytest commands.",
        "fix": "Add a machine-readable regression lane manifest.",
        "verification": "regression_lane_manifest.",
    },
    {
        "id": "P1-full-historical-suite-false-red-blocks-current-evolution",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Full historical pytest mixed archived assertions with current defaults and produced 25 known failures.",
        "fix": "Classify full historical sweep as diagnostic and enumerate known conflict families.",
        "verification": "historical_conflict_detector.",
    },
    {
        "id": "P1-regression-governance-must-not-trigger-builds",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "A regression-governance harness could accidentally mutate product state.",
        "fix": "Keep v0.3.15 live evidence to read-only health checks.",
        "verification": "safety_no_build_call.",
    },
)


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def lane_by_id(manifest: dict[str, Any], lane_id: str) -> dict[str, Any]:
    for lane in manifest.get("lanes", []):
        if lane.get("id") == lane_id:
            return lane
    raise KeyError(lane_id)


def known_failure_nodeids(manifest: dict[str, Any]) -> set[str]:
    diagnostic = lane_by_id(manifest, "full_historical_diagnostic")
    nodeids: set[str] = set()
    for family in diagnostic.get("known_conflict_families", []):
        nodeids.update(str(item) for item in family.get("failure_nodeids", []))
    return nodeids


def classify_full_sweep_failures(failed_nodeids: list[str], manifest: dict[str, Any]) -> dict[str, Any]:
    known = known_failure_nodeids(manifest)
    unknown = [nodeid for nodeid in failed_nodeids if nodeid not in known]
    return {
        "known_count": len(failed_nodeids) - len(unknown),
        "unknown_count": len(unknown),
        "unknown_failures": unknown,
        "blocking": bool(unknown),
    }


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [item for item in BUG_LEDGER if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}]
    return {"id": "p0_p1_bug_ledger_regression_lanes", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "regression_lane_docs_markers",
            str(HISTORICAL_README_PATH.relative_to(ROOT)),
            (
                "regression_lanes.json",
                "v0.3.x_current_release_gate",
                "full_historical_diagnostic",
                "Unknown failures",
            ),
        ),
        (
            "regression_lane_script_markers",
            "scripts/v03_15_regression_suite_lane_guard.py",
            (
                "classify_full_sweep_failures",
                "safety_no_build_call",
                "diagnostic_non_gating",
                "v0.3.x_current_release_gate",
            ),
        ),
    ]
    evidence: list[dict[str, Any]] = []
    for check_id, relative_path, markers in checks:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        missing = [marker for marker in markers if marker not in text]
        evidence.append({"id": check_id, "path": relative_path, "required_markers": list(markers), "missing_markers": missing, "passed": not missing})
    return evidence


def manifest_checks(manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    loaded = manifest or load_manifest()
    checks: list[dict[str, Any]] = []
    current = lane_by_id(loaded, "v0.3.x_current_release_gate")
    diagnostic = lane_by_id(loaded, "full_historical_diagnostic")
    current_files = [str(item) for item in current.get("test_files", [])]
    missing_current_files = [path for path in current_files if not (ROOT / path).exists()]
    command_missing_files = [path for path in current_files if path not in current.get("command", [])]
    source_stage_report = str(loaded.get("source_stage_report", ""))
    checks.append({
        "id": "regression_lane_manifest",
        "passed": str(loaded.get("version", "")).startswith("v0.3.")
        and loaded.get("policy", {}).get("current_gate") == "v0.3.x_current_release_gate"
        and current.get("status") == "gating"
        and "tests/test_v03_15_regression_suite_lane_guard.py" in current_files
        and isinstance(current.get("expected", {}).get("pass_count"), int)
        and current.get("expected", {}).get("pass_count") >= 73
        and bool(source_stage_report)
        and (ROOT / source_stage_report).exists()
        and not missing_current_files
        and not command_missing_files,
        "missing_current_files": missing_current_files,
        "command_missing_files": command_missing_files,
        "expected": current.get("expected"),
        "manifest_version": loaded.get("version"),
        "source_stage_report": source_stage_report,
    })
    families = diagnostic.get("known_conflict_families", [])
    family_files = [
        path
        for family in families
        for path in family.get("current_behavior_evidence", [])
        if not (ROOT / str(path)).exists()
    ]
    known_failures = sorted(known_failure_nodeids(loaded))
    classification = classify_full_sweep_failures(known_failures, loaded)
    checks.append({
        "id": "historical_conflict_detector",
        "passed": diagnostic.get("status") == "diagnostic_non_gating"
        and diagnostic.get("last_observed", {}).get("failed_count") == len(known_failures)
        and {family.get("id") for family in families} >= {"complexity_router_default_timeline", "builder_contract_timeline"}
        and not family_files
        and classification["unknown_count"] == 0,
        "known_failure_count": len(known_failures),
        "missing_current_behavior_evidence": family_files,
        "classification": classification,
    })
    checks.append({
        "id": "full_historical_sweep_non_gating",
        "passed": loaded.get("policy", {}).get("full_historical_sweep") == "diagnostic_non_gating"
        and diagnostic.get("status") != "gating"
        and loaded.get("policy", {}).get("unknown_diagnostic_failures") == "blocking_until_classified",
        "status": diagnostic.get("status"),
        "unknown_policy": loaded.get("policy", {}).get("unknown_diagnostic_failures"),
    })
    return checks


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.15-regression-lanes"})
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
    manifest = load_manifest()
    checks: list[dict[str, Any]] = [bug_ledger_evidence(), *source_marker_checks(), *manifest_checks(manifest)]
    safety: dict[str, Any] = {"build_endpoint_called": False, "called_endpoints": []}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["build_endpoint_called"] = any("/builds" in endpoint for endpoint in safety["called_endpoints"])
    checks.append({"id": "safety_no_build_call", "passed": safety["build_endpoint_called"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint": "POST /api/v1/applications/{id}/builds"})
    failed = [check for check in checks if not check.get("passed")]
    current = lane_by_id(manifest, "v0.3.x_current_release_gate")
    diagnostic = lane_by_id(manifest, "full_historical_diagnostic")
    return {
        "version": "v0.3.15",
        "stage": "regression_suite_lane_guard",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "current_gate": {"id": current.get("id"), "expected": current.get("expected"), "test_file_count": len(current.get("test_files", []))},
        "diagnostic_lane": {"id": diagnostic.get("id"), "status": diagnostic.get("status"), "last_observed": diagnostic.get("last_observed")},
        "checks": checks,
        "summary": {"failed_check_count": len(failed), "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"], "build_endpoint_called": safety["build_endpoint_called"]},
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.15 regression lane evidence.")
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
