from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "seed_v04_13_t01e_browser.py"


def _run_fixture(database: Path) -> tuple[dict[str, object], str]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--database", str(database)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout), completed.stdout


def test_browser_fixture_uses_one_real_binding_and_four_idempotent_chains(
    tmp_path: Path,
) -> None:
    database = tmp_path / "collaboration.db"

    first, first_output = _run_fixture(database)
    second, second_output = _run_fixture(database)

    assert second == first
    assert first["fixture"] == (
        "V04-13-T01E controlled browser fixture; not production evidence"
    )
    assert first["application_id"] == "93a0b339-29ab-4e1e-a091-347ce88a0c24"
    assert first["assignment_id"] == "079a3b03-080f-5b8d-8891-153a919d8f5e"
    assert first["lilies_session_id"] == "cb283cf7-4c1f-5bd9-a943-71973ad61edd"
    reports = first["reports"]
    assert isinstance(reports, list)
    assert {
        (item["scenario"], item["status"])
        for item in reports
        if isinstance(item, dict)
    } == {
        ("awaiting_user_review", "awaiting_user_review"),
        ("needs_more_evidence", "needs_more_evidence"),
        ("developer_response", "ready_for_lilies_verification"),
        ("verification_failed", "verification_failed"),
    }
    for output in (first_output, second_output):
        assert "lcc_" not in output
        assert "access_token" not in output
        assert "credential_ref" not in output
        assert "bearer" not in output

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_channels"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_reports"
        ).fetchone()[0] == 4
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_developer_responses"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT status FROM collaboration_verification_claims"
        ).fetchone()[0] == "verification_failed"
        verification = connection.execute(
            "SELECT payload_json FROM collaboration_verifications"
        ).fetchone()
        assert verification is not None
        verification_payload = json.loads(verification["payload_json"])
        assert verification_payload["differences"] == [
            {
                "actual": (
                    "The controlled fixture oracle reports that the approval card "
                    "is missing after refresh."
                ),
                "check_id": "check:t01e-browser-fixture:refresh-persistence",
                "evidence_refs": verification_payload["differences"][0][
                    "evidence_refs"
                ],
                "expected": (
                    "The capability approval card remains visible after refresh."
                ),
            }
        ]

        report_ids = {
            item["scenario"]: item["report_id"]
            for item in reports
            if isinstance(item, dict)
        }
        expected_types = {
            "awaiting_user_review": ["report"],
            "needs_more_evidence": ["report", "approval"],
            "developer_response": [
                "report",
                "approval",
                "developer_response",
            ],
            "verification_failed": [
                "report",
                "approval",
                "developer_response",
                "control",
            ],
        }
        for scenario, report_id in report_ids.items():
            messages = connection.execute(
                """
                SELECT message_id,message_type,causal_parent_id
                FROM collaboration_messages
                WHERE correlation_id=?
                ORDER BY seq
                """,
                (report_id,),
            ).fetchall()
            assert [row["message_type"] for row in messages] == expected_types[scenario]
            assert messages[0]["causal_parent_id"] is None
            for previous, current in zip(messages, messages[1:], strict=False):
                assert current["causal_parent_id"] == previous["message_id"]
