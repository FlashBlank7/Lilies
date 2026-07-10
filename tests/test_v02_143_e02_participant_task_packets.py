from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_evidence_module():
    module_path = ROOT / "scripts" / "v02_143_e02_participant_task_packets.py"
    spec = importlib.util.spec_from_file_location("v02_143_e02_packets_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_143_packet_set_is_complete_and_grounded() -> None:
    module = load_evidence_module()
    evidence = module.build_evidence()

    assert evidence["status"] == "completed"
    assert evidence["checks"]["required_files_present"] is True
    assert evidence["checks"]["source_has_raw_and_readable_packets"] is True
    assert evidence["checks"]["raw_packet_has_prompt_and_capture_id"] is True
    assert evidence["checks"]["readable_packet_has_prompt_and_capture_id"] is True
    assert evidence["checks"]["manifest_counterbalances_order"] is True


def test_v02_143_packets_preserve_completion_boundary() -> None:
    module = load_evidence_module()
    evidence = module.build_evidence()

    assert evidence["external_participant_rows_captured"] == 0
    assert evidence["e02_true_human_panel_completed"] is False
    assert evidence["global_completion_claimed"] is False
    assert evidence["unrestricted_memory_forbidden"] is True
    assert "at least 5 real participant ids" in evidence["next_closure_requires"]


def test_v02_143_answer_key_is_not_in_participant_packets() -> None:
    packet_dir = ROOT / "docs" / "experiment-status" / "e02-human-panel" / "packets"
    raw_packet = (packet_dir / "task_packet_raw_json.md").read_text(encoding="utf-8")
    readable_packet = (packet_dir / "task_packet_readable_testframe.md").read_text(encoding="utf-8")
    answer_key = (packet_dir / "answer_key.md").read_text(encoding="utf-8")

    assert "Do not show this file to participants." in answer_key
    assert "Expected Findings" not in raw_packet
    assert "Expected Findings" not in readable_packet
    assert "Output Fields" in raw_packet
    assert "Output Fields" in readable_packet
