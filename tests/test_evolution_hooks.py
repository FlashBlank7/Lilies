from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_session_context_names_current_stage_and_task() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from evolution_hook_common import active_stage_state
        from evolution_session_context import context_message

        state = active_stage_state(ROOT)
        message = context_message(ROOT)
    finally:
        sys.path.remove(str(ROOT / "scripts"))

    assert state["stage_report"] in message
    assert state["current_task_id"] in message
    assert "validation: valid" in message
    assert "docs/PRODUCT_NORTH_STAR.md" in message
    assert "product campaign: open" in message
    assert "PRODUCT-010" in message
    assert "Workingon is intermediate evidence only" in message
    assert "highest objective" in message
    assert "external evidence unavailability limits claims" in message


def test_checkpoint_contains_current_task_but_no_next_task_authority(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from evolution_hook_common import active_stage_state
        from evolution_checkpoint import write_checkpoint

        expected_task = active_stage_state(ROOT)["current_task_id"]
        destination = write_checkpoint(
            ROOT,
            {"hook_event_name": "PreCompact"},
            destination=tmp_path / "evolution_checkpoint.json",
        )
    finally:
        sys.path.remove(str(ROOT / "scripts"))

    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["current_task_id"] == expected_task
    assert payload["purpose"].startswith("intermediate resume evidence")
    assert "separately reported individual workflows" in payload["campaign_objective"]
    assert payload["campaign_completion_status"] == "open"
    assert "SCENARIO-008" in payload["open_intent_ids"]
    assert "next_stage" not in payload
    assert "next_task" not in payload


def test_stop_guard_warns_for_pending_stage(monkeypatch) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import evolution_stop_guard as guard

        monkeypatch.setattr(
            guard,
            "active_stage_state",
            lambda _root: {
                "stage_report": "docs/stage-reports/v0.4.2_pending.md",
                "current_task_id": "V04-02-T01D",
                "closure_verdict": "pending",
                "contract_status": "locked",
                "validation_status": "valid",
                "invalid_newer_reports": "none",
            },
        )
        warning = guard.stop_warning(ROOT)
    finally:
        sys.path.remove(str(ROOT / "scripts"))

    assert "closure=pending" in warning
    assert "current task=" in warning
    assert "Do not claim stage or campaign completion" in warning
    assert "evidence ceiling is not a campaign blocker" in warning


def test_stop_guard_does_not_treat_closed_stage_as_product_completion(monkeypatch) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import evolution_stop_guard as guard

        monkeypatch.setattr(
            guard,
            "active_stage_state",
            lambda _root: {
                "stage_report": "docs/stage-reports/v0.4.11_closed.md",
                "current_task_id": "none",
                "closure_verdict": "pass",
                "contract_status": "locked",
                "validation_status": "valid",
                "invalid_newer_reports": "none",
                "campaign_completion_status": "open",
                "open_intent_ids": "PRODUCT-010, SCENARIO-008",
            },
        )
        warning = guard.stop_warning(ROOT)
    finally:
        sys.path.remove(str(ROOT / "scripts"))

    assert "Stage docs/stage-reports/v0.4.11_closed.md is closed" in warning
    assert "PRODUCT-010" in warning
    assert "Do not claim product or campaign completion" in warning


def test_latest_report_selection_skips_invalid_newer_report(tmp_path: Path, monkeypatch) -> None:
    import sys

    reports = tmp_path / "docs/stage-reports"
    reports.mkdir(parents=True)
    valid = reports / "v0.4.2_valid.md"
    invalid = reports / "v0.4.3_invalid.md"
    marker = "| Template version | `2.0` |\n## Stage Contract\n"
    valid.write_text(marker, encoding="utf-8")
    invalid.write_text(marker, encoding="utf-8")
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import evolution_hook_common as common

        monkeypatch.setattr(
            common,
            "stage_report_errors",
            lambda _root, path: ["invalid"] if path == invalid else [],
        )
        assert common.latest_v2_stage_report(tmp_path) == valid
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def test_stop_guard_does_not_trust_self_declared_pass(monkeypatch) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import evolution_stop_guard as guard

        monkeypatch.setattr(
            guard,
            "active_stage_state",
            lambda _root: {
                "stage_report": "none",
                "current_task_id": "none",
                "closure_verdict": "pass",
                "contract_status": "locked",
                "validation_status": "invalid",
                "invalid_newer_reports": "docs/stage-reports/v0.4.3_false_pass.md",
            },
        )
        warning = guard.stop_warning(ROOT)
    finally:
        sys.path.remove(str(ROOT / "scripts"))

    assert "No valid v2 stage report" in warning
    assert "Do not claim stage or campaign completion" in warning
