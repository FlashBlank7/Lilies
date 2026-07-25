from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_platform.token_monitoring import (
    collect_token_monitor_snapshot,
    discover_model_capable_processes,
    snapshot_delta,
)
from scripts import run_v04_13_enterprise_experiment as enterprise_runner


def _platform_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE platform_harness_tasks(
          id TEXT, kind TEXT, status TEXT, owner_id TEXT, resource_id TEXT,
          record_json TEXT, updated_at TEXT
        );
        CREATE TABLE applications(id TEXT, active_version INTEGER);
        CREATE TABLE application_versions(
          application_id TEXT, version INTEGER, snapshot_json TEXT
        );
        CREATE TABLE durable_jobs(
          id TEXT, application_id TEXT, status TEXT, next_attempt_at TEXT,
          attempt_count INTEGER, max_attempts INTEGER, updated_at TEXT
        );
        """
    )
    record = {
        "kind": "workflow_run",
        "usage": [
            {
                "usage_type": "model_call",
                "created_at": "2026-07-25T00:00:00+00:00",
                "metadata": {
                    "model": "deepseek-v4-flash",
                    "node_id": "model-1",
                },
            },
            {
                "usage_type": "model_usage",
                "created_at": "2026-07-25T00:00:00+00:00",
                "metadata": {
                    "phase": "workflow_model_text",
                    "task_id": "task-1",
                    "task_kind": "workflow_run",
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 3,
                    "cost_usd": 0.25,
                    "cost_source": "estimated_configured_price",
                },
            }
        ],
    }
    connection.execute(
        "INSERT INTO platform_harness_tasks VALUES(?,?,?,?,?,?,?)",
        (
            "task-1",
            "workflow_run",
            "succeeded",
            "owner",
            "resource",
            json.dumps(record),
            "2026-07-25T00:00:01+00:00",
        ),
    )
    connection.execute("INSERT INTO applications VALUES('app-1',1)")
    connection.execute(
        "INSERT INTO application_versions VALUES(?,?,?)",
        (
            "app-1",
            1,
            json.dumps(
                {
                    "workflow": {
                        "nodes": [
                            {
                                "id": "schedule",
                                "type": "schedule_trigger",
                                "title": "Daily",
                            }
                        ]
                    }
                }
            ),
        ),
    )
    connection.execute(
        "INSERT INTO durable_jobs VALUES(?,?,?,?,?,?,?)",
        (
            "job-1",
            "app-1",
            "retry_wait",
            "2026-07-24T00:00:00+00:00",
            1,
            3,
            "2026-07-24T00:00:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def _lilies_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions(
          id TEXT, status TEXT, assignment_id TEXT, config_json TEXT,
          assignment_json TEXT, token_count INTEGER, cost_usd REAL,
          tool_count INTEGER, model_call_count INTEGER,
          created_at TEXT, updated_at TEXT
        );
        CREATE TABLE turns(
          id TEXT, session_id TEXT, status TEXT, phase TEXT, token_count INTEGER,
          cost_usd REAL, tool_count INTEGER, model_call_count INTEGER,
          checkpoint_json TEXT, created_at TEXT, updated_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "session-1",
            "waiting_collaboration",
            "assignment-1",
            json.dumps({"kind": "platform", "model": "deepseek-v4-flash"}),
            json.dumps({"mode": "formal", "collaboration": {"channel_id": "channel"}}),
            25,
            0.5,
            2,
            1,
            "2026-07-25T00:00:00+00:00",
            "2026-07-25T00:01:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "turn-1",
            "session-1",
            "waiting_collaboration",
            "waiting_collaboration",
            25,
            0.5,
            2,
            1,
            json.dumps(
                {
                    "metrics": {
                        "usage": {
                            "input_tokens": 20,
                            "output_tokens": 5,
                            "cache_read_input_tokens": 7,
                            "cost_usd": 0.5,
                            "cost_source": "estimated_configured_price",
                        },
                        "usage_backed_model_calls": 1,
                    },
                    "pending": {"kind": "collaboration_result_recovered"},
                }
            ),
            "2026-07-25T00:00:00+00:00",
            "2026-07-25T00:01:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def _bridge_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE local_lilies_assignments(
          assignment_id TEXT, session_id TEXT, status TEXT, phase TEXT,
          desired_state TEXT, updated_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO local_lilies_assignments VALUES(?,?,?,?,?,?)",
        (
            "assignment-1",
            "session-1",
            "running",
            "running",
            "active",
            "2026-07-25T00:01:00+00:00",
        ),
    )
    connection.commit()
    connection.close()


def _development_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE collaborative_development_assignments(
          assignment_id TEXT, status TEXT, execution_mode TEXT, updated_at TEXT
        );
        CREATE TABLE collaborative_development_provider_cost_reservations(
          reservation_id TEXT, assignment_id TEXT, provider TEXT, status TEXT,
          record_json TEXT, reserved_at TEXT, settled_at TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO collaborative_development_assignments VALUES(?,?,?,?)",
        ("dev-1", "active", "autonomous", "2026-07-25T00:00:00+00:00"),
    )
    connection.execute(
        "INSERT INTO collaborative_development_provider_cost_reservations "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "reservation-1",
            "dev-1",
            "deepseek",
            "settled",
            json.dumps(
                {
                    "receipt": {
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "cost_usd": 0.1,
                        "model": "deepseek-v4-flash",
                    }
                }
            ),
            "2026-07-25T00:00:00+00:00",
            "2026-07-25T00:00:01+00:00",
        ),
    )
    connection.commit()
    connection.close()


def test_monitor_combines_ledgers_and_reports_startup_risk(tmp_path: Path) -> None:
    platform = tmp_path / "platform.db"
    lilies = tmp_path / "lilies.db"
    bridge = tmp_path / "bridge.db"
    development = tmp_path / "development.db"
    _platform_db(platform)
    _lilies_db(lilies)
    _bridge_db(bridge)
    _development_db(development)

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        lilies_db=lilies,
        bridge_db=bridge,
        development_db=development,
        process_rows=[],
        generated_at="2026-07-25T01:00:00+00:00",
    )

    assert result["usage"]["totals"] == {
        "input_tokens": 33,
        "output_tokens": 11,
        "cached_input_tokens": 10,
        "reasoning_tokens": 0,
        "unattributed_tokens": 0,
        "tokens": 44,
        "cost_usd": 0.85,
        "model_calls": 3,
        "usage_records": 3,
        "unknown_usage_model_calls": 0,
    }
    assert {item["name"] for item in result["usage"]["by_stage"]} == {
        "workflow_model_text",
        "local_lilies_collaboration",
        "collaborative_development",
    }
    assert result["safety"]["safe_now"] is True
    assert result["safety"]["safe_on_platform_or_daemon_start"] is False
    assert result["safety"]["startup_auto_consumers"] == {
        "published_schedule_nodes": 1,
        "due_durable_jobs": 1,
        "recoverable_local_assignments": 1,
        "startup_resumable_lilies_turns": 1,
        "active_autonomous_development_assignments": 1,
    }


def test_platform_model_call_without_usage_is_counted_unknown(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "platform.db"
    _platform_db(platform)
    connection = sqlite3.connect(platform)
    row = connection.execute(
        "SELECT record_json FROM platform_harness_tasks WHERE id='task-1'"
    ).fetchone()
    record = json.loads(row[0])
    record["usage"].append(
        {
            "usage_type": "model_call",
            "created_at": "2026-07-25T00:00:02+00:00",
            "metadata": {
                "model": "deepseek-v4-flash",
                "node_id": "model-1",
            },
        }
    )
    connection.execute(
        "UPDATE platform_harness_tasks SET status='failed',record_json=? "
        "WHERE id='task-1'",
        (json.dumps(record),),
    )
    connection.commit()
    connection.close()

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        lilies_db=tmp_path / "missing-lilies.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        required_sources=("platform",),
        process_rows=[],
    )

    totals = result["usage"]["totals"]
    assert totals["model_calls"] == 2
    assert totals["usage_records"] == 1
    assert totals["unknown_usage_model_calls"] == 1


def test_missing_required_ledger_makes_safety_unknown(tmp_path: Path) -> None:
    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        lilies_db=tmp_path / "missing-lilies.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        process_rows=[],
        model_egress_enabled=False,
    )

    assert result["safety"]["evidence_complete"] is False
    assert result["safety"]["safe_now"] is None
    assert result["safety"]["safe_on_platform_or_daemon_start"] is None
    assert result["safety"]["missing_required_sources"] == [
        "platform",
        "local_lilies",
        "bridge",
        "collaborative_development",
    ]


def test_process_detection_and_delta() -> None:
    rows = [
        "123 1 00:10 python -m agent_platform.lilies_cli serve --port 8765",
        "124 1 00:10 node next dev",
    ]
    processes = discover_model_capable_processes(rows)
    assert [(item["pid"], item["kind"]) for item in processes] == [
        (123, "local_lilies_daemon")
    ]

    previous = {
        "usage": {
            "totals": {
                "input_tokens": 10,
                "output_tokens": 2,
                "unattributed_tokens": 0,
                "tokens": 12,
                "cost_usd": 0.1,
                "model_calls": 1,
                "usage_records": 1,
                "unknown_usage_model_calls": 0,
            }
        }
    }
    current = {
        "usage": {
            "totals": {
                "input_tokens": 20,
                "output_tokens": 7,
                "unattributed_tokens": 0,
                "tokens": 27,
                "cost_usd": 0.25,
                "model_calls": 3,
                "usage_records": 3,
                "unknown_usage_model_calls": 0,
            }
        }
    }
    delta = snapshot_delta(previous, current, elapsed_seconds=30)
    assert delta["input_tokens"] == 10
    assert delta["output_tokens"] == 5
    assert delta["tokens"] == 15
    assert delta["model_calls"] == 2
    assert delta["unknown_usage_model_calls"] == 0
    assert delta["reconciled_unknown_usage_model_calls"] == 0
    assert delta["cost_usd"] == 0.15
    assert delta["tokens_per_minute"] == 30


def test_active_local_turn_uses_checkpoint_call_counters(tmp_path: Path) -> None:
    lilies = tmp_path / "lilies.db"
    _lilies_db(lilies)
    connection = sqlite3.connect(lilies)
    connection.execute(
        "UPDATE sessions SET status='running',token_count=0,cost_usd=0,"
        "tool_count=0,model_call_count=0 WHERE id='session-1'"
    )
    connection.execute(
        "UPDATE turns SET status='running',token_count=0,cost_usd=0,"
        "tool_count=0,model_call_count=0,checkpoint_json=? WHERE id='turn-1'",
        (
            json.dumps(
                {
                    "metrics": {
                        "model_calls": 57,
                        "tool_calls": 77,
                        "usage_backed_model_calls": 55,
                        "usage": {
                            "input_tokens": 7_803_173,
                            "output_tokens": 34_003,
                            "cache_read_input_tokens": 405_376,
                            "cost_usd": 1.101965,
                            "cost_source": "estimated_configured_price",
                        },
                    }
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        lilies_db=lilies,
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        required_sources=("local_lilies",),
        process_rows=[],
    )

    totals = result["usage"]["totals"]
    assert totals["model_calls"] == 57
    assert totals["unknown_usage_model_calls"] == 2
    assert totals["tokens"] == 7_837_176
    assert result["sources"]["local_lilies"]["active_sessions"] == [
        {
            "session_id": "session-1",
            "assignment_id": "assignment-1",
            "status": "running",
            "stage": "local_lilies_collaboration",
            "tokens": 7_837_176,
            "cost_usd": 1.101965,
            "model_calls": 57,
            "tool_calls": 77,
            "updated_at": "2026-07-25T00:01:00+00:00",
        }
    ]


def test_unknown_usage_delta_labels_late_receipt_reconciliation() -> None:
    previous = {
        "usage": {
            "totals": {
                "model_calls": 57,
                "unknown_usage_model_calls": 2,
            }
        }
    }
    current = {
        "usage": {
            "totals": {
                "model_calls": 57,
                "unknown_usage_model_calls": 0,
            }
        }
    }

    delta = snapshot_delta(previous, current, elapsed_seconds=5)

    assert delta["model_calls"] == 0
    assert delta["unknown_usage_model_calls"] == 0
    assert delta["reconciled_unknown_usage_model_calls"] == 2


def test_enterprise_runner_persists_private_live_monitor_snapshot(
    tmp_path: Path,
) -> None:
    history = tmp_path / "monitoring" / "token-monitor.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("")
    history.chmod(0o644)
    snapshot, observed_at = enterprise_runner._record_token_monitor_snapshot(
        tmp_path,
        previous=None,
        previous_at=10.0,
        observed_at=15.0,
    )

    latest = tmp_path / "monitoring" / "token-monitor.latest.json"
    latest_payload = json.loads(latest.read_bytes())
    history_payload = json.loads(history.read_text().strip())
    assert observed_at == 15.0
    assert snapshot["usage"]["totals"]["tokens"] == 0
    assert latest_payload == history_payload
    assert latest_payload["totals"]["tokens"] == 0
    assert latest.stat().st_mode & 0o777 == 0o600
    assert history.stat().st_mode & 0o777 == 0o600


def test_enterprise_runner_forces_terminal_monitor_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []

    def fail_poll(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise enterprise_runner.EnterpriseExperimentError("terminal failure")

    def record(
        state_root: Path,
        *,
        previous: dict[str, object] | None,
        previous_at: float,
        observed_at: float,
    ) -> tuple[dict[str, object], float]:
        observed.append(state_root)
        return {"usage": {"totals": {}}}, observed_at

    monkeypatch.setattr(enterprise_runner, "_poll_assignment_inner", fail_poll)
    monkeypatch.setattr(
        enterprise_runner,
        "_record_token_monitor_snapshot",
        record,
    )

    with pytest.raises(
        enterprise_runner.EnterpriseExperimentError,
        match="terminal failure",
    ):
        enterprise_runner._poll_assignment(
            "http://127.0.0.1:1",
            "token",
            assignment_id="assignment",
            deadline_seconds=1,
            token_state_root=tmp_path,
            token_monitor_interval=5,
        )

    assert observed == [tmp_path]


def test_local_model_call_without_usage_is_unknown_not_zero(tmp_path: Path) -> None:
    lilies = tmp_path / "lilies.db"
    _lilies_db(lilies)
    connection = sqlite3.connect(lilies)
    connection.execute(
        "UPDATE sessions SET token_count=25,model_call_count=2 WHERE id='session-1'"
    )
    connection.execute(
        "UPDATE turns SET model_call_count=2,checkpoint_json=? WHERE id='turn-1'",
        (json.dumps({"metrics": {}, "pending": {}}),),
    )
    connection.commit()
    connection.close()

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        lilies_db=lilies,
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        process_rows=[],
    )

    totals = result["usage"]["totals"]
    assert totals["model_calls"] == 2
    assert totals["usage_records"] == 1
    assert totals["unknown_usage_model_calls"] == 2
    assert totals["tokens"] == 25
