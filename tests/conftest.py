from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REGRESSION_MANIFEST = ROOT / "docs/testing/regression_lanes.json"


def archived_expectation_conflicts(path: Path = REGRESSION_MANIFEST) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    conflicts: dict[str, str] = {}
    for lane in manifest.get("lanes", []):
        if lane.get("id") != "full_historical_diagnostic":
            continue
        for family in lane.get("known_conflict_families", []):
            if family.get("classification") != "archived_expectation_conflict":
                continue
            family_id = str(family.get("id", "unknown"))
            for nodeid in family.get("failure_nodeids", []):
                conflicts[str(nodeid)] = family_id
    return conflicts


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    conflicts = archived_expectation_conflicts()
    for item in items:
        nodeid = item.nodeid.split("[", 1)[0]
        family_id = conflicts.get(nodeid)
        if family_id is None:
            continue
        item.add_marker(pytest.mark.xfail(
            strict=True,
            reason=f"archived expectation conflict: {family_id}",
        ))
