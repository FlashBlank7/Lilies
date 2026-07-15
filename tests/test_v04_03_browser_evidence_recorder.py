from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.v04_03_browser_closure_gate import load_json, validate_browser_closure
from scripts.v04_03_browser_evidence_recorder import (
    finalize,
    record_browser,
    record_console,
    record_interaction,
    record_overlap,
    record_screenshot,
    save_json_atomic,
)
from tests.test_v04_03_browser_closure_gate import _passing_evidence


ROOT = Path(__file__).resolve().parents[1]
JOURNEY = load_json(ROOT / "docs/workingon/v0.4.3_browser_journey.json")
BLOCKED = load_json(ROOT / "docs/workingon/v0.4.3_browser_verification.json")


def test_partial_recorder_updates_always_remain_blocked(tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"
    updated = record_browser(BLOCKED, "iab")
    updated = record_interaction(
        JOURNEY,
        updated,
        interaction_id="mode_policy",
        viewport="desktop",
        status="passed",
    )
    save_json_atomic(evidence_path, updated)

    persisted = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert persisted["status"] == "blocked"
    assert persisted["browser_discovery"]["selected_browser"] == "iab"
    assert persisted["interactions"]["mode_policy"]["desktop"] == "passed"
    assert not (tmp_path / ".evidence.json.tmp").exists()


def test_screenshot_recorder_hashes_only_versioned_png_evidence(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    screenshot = evidence["desktop"]["screenshots"][0]
    screenshot_path = tmp_path / screenshot["path"]
    updated = record_screenshot(
        tmp_path,
        JOURNEY,
        BLOCKED,
        screenshot_id=screenshot["id"],
        viewport="desktop",
        path=screenshot_path,
    )

    assert updated["status"] == "blocked"
    assert updated["desktop"]["screenshots"][0]["sha256"] == screenshot["sha256"]
    with pytest.raises(ValueError, match="versioned screenshot root"):
        record_screenshot(
            tmp_path,
            JOURNEY,
            BLOCKED,
            screenshot_id=screenshot["id"],
            viewport="desktop",
            path=tmp_path / "outside.png",
        )


def test_finalize_is_the_only_path_to_passed_evidence(tmp_path: Path) -> None:
    evidence = _passing_evidence(tmp_path)
    evidence["status"] = "blocked"
    evidence = record_console(evidence, [])
    evidence = record_overlap(evidence, status="passed")

    finalized, errors = finalize(root=tmp_path, journey=JOURNEY, evidence=evidence)

    assert errors == []
    assert finalized["status"] == "passed"
    assert validate_browser_closure(root=tmp_path, journey=JOURNEY, evidence=finalized) == []

    incomplete, errors = finalize(root=ROOT, journey=JOURNEY, evidence=BLOCKED)
    assert incomplete["status"] == "blocked"
    assert errors
