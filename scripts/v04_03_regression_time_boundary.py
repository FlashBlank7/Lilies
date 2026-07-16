#!/usr/bin/env python3
"""Classify a pytest JUnit result against the v0.4.x regression policy."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/testing/regression_lanes.json"
DEFAULT_OUTPUT = ROOT / "docs/workingon/v0.4.3_full_suite_failure_inventory.json"


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def conflict_map(manifest: dict[str, Any]) -> dict[str, dict[str, str]]:
    conflicts: dict[str, dict[str, str]] = {}
    for lane in manifest.get("lanes", []):
        if lane.get("id") != "full_historical_diagnostic":
            continue
        for family in lane.get("known_conflict_families", []):
            family_id = str(family.get("id", "unknown"))
            classification = str(family.get("classification", "current_regression"))
            reason = str(family.get("reason", ""))
            for nodeid in family.get("failure_nodeids", []):
                conflicts[str(nodeid)] = {
                    "classification": classification,
                    "family_id": family_id,
                    "reason": reason,
                }
    return conflicts


def testcase_nodeid(classname: str, name: str) -> str:
    parts = classname.split(".")
    while parts:
        candidate = ROOT / ("/".join(parts) + ".py")
        if candidate.exists():
            return f"{candidate.relative_to(ROOT).as_posix()}::{name}"
        parts.pop()
    return f"{classname}::{name}"


def junit_results(path: Path) -> tuple[dict[str, int], list[dict[str, str]], list[dict[str, str]]]:
    root = ET.parse(path).getroot()
    suite = root.find("testsuite") if root.tag == "testsuites" else root
    if suite is None:
        raise ValueError("JUnit file has no testsuite")
    totals = {
        key: int(float(suite.attrib.get(key, "0")))
        for key in ("tests", "failures", "errors", "skipped")
    }
    failures: list[dict[str, str]] = []
    expected_conflicts: list[dict[str, str]] = []
    for case in suite.iter("testcase"):
        nodeid = testcase_nodeid(case.attrib.get("classname", ""), case.attrib.get("name", ""))
        skipped = case.find("skipped")
        if skipped is not None and skipped.attrib.get("type") == "pytest.xfail":
            expected_conflicts.append({
                "nodeid": nodeid,
                "reason": skipped.attrib.get("message", ""),
            })
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue
        failures.append({
            "nodeid": nodeid,
            "message": failure.attrib.get("message", "") or (failure.text or "").strip(),
        })
    return totals, failures, expected_conflicts


def classify(junit_path: Path, manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    totals, failures, expected_conflicts = junit_results(junit_path)
    known = conflict_map(manifest)
    rows: list[dict[str, str]] = []
    for failure in failures:
        policy = known.get(failure["nodeid"])
        if policy is None:
            policy = {
                "classification": "current_regression",
                "family_id": "unclassified_current_failure",
                "reason": "No archived or environment classification exists; current stage owns remediation.",
            }
        rows.append({**failure, **policy})
    counts = Counter(row["classification"] for row in rows)
    expected_family_counts = Counter(
        known[row["nodeid"]]["family_id"]
        for row in expected_conflicts
        if row["nodeid"] in known
    )
    return {
        "schema_version": "1.1",
        "stage": str(manifest.get("version", "v0.4.x")),
        "source_junit": str(junit_path),
        "manifest": str(manifest_path),
        "totals": totals,
        "classification_counts": dict(sorted(counts.items())),
        "blocking_current_regressions": [
            row["nodeid"] for row in rows if row["classification"] == "current_regression"
        ],
        "expected_conflicts": expected_conflicts,
        "expected_conflict_count": len(expected_conflicts),
        "expected_conflict_family_counts": dict(sorted(expected_family_counts.items())),
        "unknown_expected_conflicts": [
            row["nodeid"] for row in expected_conflicts if row["nodeid"] not in known
        ],
        "missing_expected_conflicts": sorted(
            set(known) - {row["nodeid"] for row in expected_conflicts} - {row["nodeid"] for row in failures}
        ),
        "failures": rows,
    }


def write_inventory(path: Path, inventory: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("junit", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    inventory = classify(args.junit, args.manifest)
    write_inventory(args.output, inventory)
    print(json.dumps({
        "output": str(args.output),
        "stage": inventory["stage"],
        "totals": inventory["totals"],
        "classification_counts": inventory["classification_counts"],
        "expected_conflict_count": inventory["expected_conflict_count"],
    }, ensure_ascii=False))
    invalid = (
        inventory["blocking_current_regressions"]
        or inventory["unknown_expected_conflicts"]
        or inventory["missing_expected_conflicts"]
    )
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
