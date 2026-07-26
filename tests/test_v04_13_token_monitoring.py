from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_platform import token_monitoring
from agent_platform.token_monitoring import (
    collect_token_monitor_snapshot,
    compact_token_monitor_snapshot,
    discover_model_capable_processes,
    snapshot_delta,
)
from scripts import monitor_lilies_tokens as token_monitor_cli
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
            },
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
        "INSERT INTO collaborative_development_provider_cost_reservations VALUES(?,?,?,?,?,?,?)",
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


def _checkpointed_wal_bridge_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    connection.execute(
        """
        CREATE TABLE local_lilies_assignments(
          assignment_id TEXT, session_id TEXT, status TEXT, phase TEXT,
          desired_state TEXT, updated_at TEXT
        )
        """
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    Path(f"{path}-shm").unlink(missing_ok=True)
    Path(f"{path}-wal").unlink(missing_ok=True)


def _standalone_usage_payload(
    *,
    items: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    usage_items = items or []
    return {
        "schema_version": "1.0",
        "group_by": ["session", "stage", "model"],
        "items": usage_items,
        "page": 1,
        "page_size": 100,
        "returned_count": len(usage_items),
        "total_items": len(usage_items),
        "total_pages": 1 if usage_items else 0,
        "truncated": False,
    }


def _standalone_observability_receipt(
    *,
    recorded_calls: int = 2,
    unknown_calls: int = 1,
    active_provider_calls: int = 0,
    input_tokens: int = 100,
    output_tokens: int = 23,
    cost_usd: float = 0.5,
    model_egress_enabled: bool = False,
    automatic_model_resume_count: int = 0,
    explicit_resume_candidate_count: int = 0,
    active_sessions: int | None = None,
    active_model_turns: int | None = None,
    active_development_model_calls: int = 0,
    captured_at: str = "2026-07-25T01:00:00+00:00",
    activity_revision: int = 7,
) -> dict[str, object]:
    resolved_model_turns = (
        1 if active_provider_calls else 0
    ) if active_model_turns is None else active_model_turns
    resolved_sessions = (
        resolved_model_turns if active_sessions is None else active_sessions
    )
    attempted_calls = recorded_calls + unknown_calls + active_provider_calls
    return {
        "schema_version": "1.0",
        "scope": "daemon_global",
        "coverage_complete": True,
        "daemon_fingerprint": "sha256:" + "a" * 64,
        "daemon_instance_id": "e8be0136-9185-41a6-81e8-f7c9a2bfce76",
        "captured_at": captured_at,
        "activity_revision": activity_revision,
        "model_egress_enabled": model_egress_enabled,
        "usage": {
            "attempted_calls": attempted_calls,
            "recorded_calls": recorded_calls,
            "unknown_calls": unknown_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": cost_usd,
            "ledger_cursor": max(20, attempted_calls),
        },
        "runtime": {
            "active_sessions": resolved_sessions,
            "active_model_turns": resolved_model_turns,
            "active_provider_calls": active_provider_calls,
            "active_development_model_calls": active_development_model_calls,
        },
        "startup": {
            "recovery_completed": True,
            "automatic_resume_policy": "explicit_request_only",
            "automatic_model_resume_count": automatic_model_resume_count,
            "explicit_resume_candidate_count": explicit_resume_candidate_count,
            "interrupted_sessions": explicit_resume_candidate_count,
            "interrupted_turns": explicit_resume_candidate_count,
            "interrupted_development_assignments": 0,
            "reconciliation_required_development_invocations": 0,
        },
    }


def _standalone_observability_envelope(
    *,
    usage: dict[str, object] | None = None,
    **receipt_kwargs: object,
) -> dict[str, object]:
    before = _standalone_observability_receipt(**receipt_kwargs)
    after = json.loads(json.dumps(before))
    after["captured_at"] = "2026-07-25T01:00:01+00:00"
    after["activity_revision"] = int(before["activity_revision"]) + 1
    client_acl_usage = dict(usage) if usage is not None else _standalone_usage_payload()
    client_acl_usage["snapshot_kind"] = "complete_paginated_merge"
    return {
        "schema_version": "1.0",
        "snapshot_kind": "paired_observability_bracket",
        "before": before,
        "client_acl_usage": client_acl_usage,
        "after": after,
    }


def _empty_startup_databases(tmp_path: Path) -> tuple[Path, Path, Path]:
    platform = tmp_path / "platform.db"
    bridge = tmp_path / "bridge.db"
    development = tmp_path / "development.db"
    _platform_db(platform)
    _bridge_db(bridge)
    _development_db(development)
    for path, tables in (
        (
            platform,
            (
                "platform_harness_tasks",
                "applications",
                "application_versions",
                "durable_jobs",
            ),
        ),
        (bridge, ("local_lilies_assignments",)),
        (
            development,
            (
                "collaborative_development_assignments",
                "collaborative_development_provider_cost_reservations",
            ),
        ),
    ):
        connection = sqlite3.connect(path)
        for table in tables:
            connection.execute(f"DELETE FROM {table}")
        connection.commit()
        connection.close()
    return platform, bridge, development


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
        platform_owned_legacy_lilies_db=lilies,
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
        "startup_resumable_standalone_lilies_turns": None,
        "active_autonomous_development_assignments": 1,
    }

    model_off = collect_token_monitor_snapshot(
        platform_db=platform,
        platform_owned_legacy_lilies_db=lilies,
        bridge_db=bridge,
        development_db=development,
        process_rows=[],
        model_egress_enabled=False,
        generated_at="2026-07-25T01:00:00+00:00",
    )
    assert model_off["safety"]["safe_on_platform_or_daemon_start"] is False
    assert model_off["safety"]["model_egress_configuration_scope"] == (
        "monitor_default_only_not_a_process_attestation"
    )


def test_monitor_reads_checkpointed_wal_without_creating_auxiliary_files(
    tmp_path: Path,
) -> None:
    bridge = tmp_path / "bridge.db"
    _checkpointed_wal_bridge_db(bridge)

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        platform_owned_legacy_lilies_db=tmp_path / "missing-lilies.db",
        bridge_db=bridge,
        development_db=tmp_path / "missing-development.db",
        required_sources=("bridge",),
        process_rows=[],
    )

    assert result["sources"]["bridge"]["available"] is True
    assert result["safety"]["evidence_complete"] is True
    assert not Path(f"{bridge}-shm").exists()
    assert not Path(f"{bridge}-wal").exists()


def test_monitor_includes_committed_live_wal_usage(tmp_path: Path) -> None:
    platform = tmp_path / "platform.db"
    _platform_db(platform)
    writer = sqlite3.connect(platform)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    record = json.loads(
        writer.execute(
            "SELECT record_json FROM platform_harness_tasks WHERE id='task-1'"
        ).fetchone()[0]
    )
    usage = next(item for item in record["usage"] if item["usage_type"] == "model_usage")
    usage["metadata"]["input_tokens"] = 123
    usage["metadata"]["output_tokens"] = 0
    writer.execute(
        "UPDATE platform_harness_tasks SET record_json=? WHERE id='task-1'",
        (json.dumps(record),),
    )
    writer.commit()
    assert Path(f"{platform}-wal").stat().st_size > 0

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        required_sources=("platform",),
        process_rows=[],
    )
    writer.close()

    assert result["sources"]["platform"]["available"] is True
    assert result["usage"]["totals"]["tokens"] == 123
    assert result["safety"]["evidence_complete"] is True


def test_wal_appearing_during_snapshot_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = tmp_path / "bridge.db"
    _checkpointed_wal_bridge_db(bridge)
    original_copy = token_monitoring._copy_snapshot_component
    writers: list[sqlite3.Connection] = []
    raced = False

    def copy_with_race(
        source: Path,
        destination: Path,
        *,
        expected_identity: tuple[int, int, int, int, int],
    ) -> None:
        nonlocal raced
        original_copy(
            source,
            destination,
            expected_identity=expected_identity,
        )
        if not raced and source == bridge:
            raced = True
            writer = sqlite3.connect(bridge)
            writer.execute(
                "INSERT INTO local_lilies_assignments VALUES(?,?,?,?,?,?)",
                ("race", "session", "running", "running", "active", "now"),
            )
            writer.commit()
            writers.append(writer)

    monkeypatch.setattr(
        token_monitoring,
        "_copy_snapshot_component",
        copy_with_race,
    )
    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=bridge,
        development_db=tmp_path / "missing-development.db",
        required_sources=("bridge",),
        process_rows=[],
    )
    for writer in writers:
        writer.close()

    assert raced is True
    assert result["sources"]["bridge"]["available"] is False
    assert result["sources"]["bridge"]["unavailable_reason"] == "read_error"
    assert result["safety"]["evidence_complete"] is False
    assert result["safety"]["safe_now"] is None


@pytest.mark.parametrize(
    ("source_name", "path_argument"),
    [
        ("platform", "platform_db"),
        ("platform_owned_legacy_lilies", "platform_owned_legacy_lilies_db"),
        ("bridge", "bridge_db"),
        ("collaborative_development", "development_db"),
    ],
)
def test_readable_wrong_sqlite_schema_is_not_available(
    tmp_path: Path,
    source_name: str,
    path_argument: str,
) -> None:
    unrelated = tmp_path / f"{source_name}.db"
    connection = sqlite3.connect(unrelated)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.commit()
    connection.close()
    arguments: dict[str, object] = {
        "platform_db": tmp_path / "missing-platform.db",
        "bridge_db": tmp_path / "missing-bridge.db",
        "development_db": tmp_path / "missing-development.db",
        "required_sources": (source_name,),
        "process_rows": [],
    }
    arguments[path_argument] = unrelated

    result = collect_token_monitor_snapshot(**arguments)

    source = result["sources"][source_name]
    assert source["available"] is False
    assert source["schema_valid"] is False
    assert source["unavailable_reason"] == "schema_mismatch"
    assert source["schema_errors"]
    assert result["safety"]["evidence_complete"] is False
    assert result["safety"]["safe_now"] is None


def test_malformed_persisted_accounting_fails_sources_closed(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "platform.db"
    legacy = tmp_path / "legacy.db"
    development = tmp_path / "development.db"
    _platform_db(platform)
    _lilies_db(legacy)
    _development_db(development)

    connection = sqlite3.connect(platform)
    record = json.loads(
        connection.execute(
            "SELECT record_json FROM platform_harness_tasks WHERE id='task-1'"
        ).fetchone()[0]
    )
    model_usage = next(item for item in record["usage"] if item["usage_type"] == "model_usage")
    model_usage["metadata"]["input_tokens"] = "corrupt"
    connection.execute(
        "UPDATE platform_harness_tasks SET record_json=? WHERE id='task-1'",
        (json.dumps(record),),
    )
    connection.commit()
    connection.close()

    connection = sqlite3.connect(legacy)
    connection.execute("UPDATE sessions SET token_count=-1 WHERE id='session-1'")
    connection.commit()
    connection.close()

    connection = sqlite3.connect(development)
    connection.execute(
        "UPDATE collaborative_development_provider_cost_reservations "
        "SET record_json=? WHERE reservation_id='reservation-1'",
        (
            json.dumps(
                {
                    "receipt": {
                        "input_tokens": 3,
                        "output_tokens": 2,
                        "cost_usd": float("nan"),
                    }
                }
            ),
        ),
    )
    connection.commit()
    connection.close()

    common = {
        "bridge_db": tmp_path / "missing-bridge.db",
        "process_rows": [],
    }
    cases = [
        (
            "platform",
            collect_token_monitor_snapshot(
                platform_db=platform,
                development_db=tmp_path / "missing-development.db",
                required_sources=("platform",),
                **common,
            ),
        ),
        (
            "platform_owned_legacy_lilies",
            collect_token_monitor_snapshot(
                platform_db=tmp_path / "missing-platform.db",
                platform_owned_legacy_lilies_db=legacy,
                development_db=tmp_path / "missing-development.db",
                required_sources=("platform_owned_legacy_lilies",),
                **common,
            ),
        ),
        (
            "collaborative_development",
            collect_token_monitor_snapshot(
                platform_db=tmp_path / "missing-platform.db",
                development_db=development,
                bridge_db=tmp_path / "missing-bridge.db",
                required_sources=("collaborative_development",),
                process_rows=[],
            ),
        ),
    ]
    for source_name, result in cases:
        assert result["sources"][source_name]["available"] is False
        assert result["sources"][source_name]["schema_valid"] is False
        assert result["sources"][source_name]["unavailable_reason"] == "data_mismatch"
        assert result["sources"][source_name]["schema_errors"]
        assert result["safety"]["ledger_evidence_complete"] is False
        assert result["safety"]["safe_now"] is None


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
        "UPDATE platform_harness_tasks SET status='failed',record_json=? WHERE id='task-1'",
        (json.dumps(record),),
    )
    connection.commit()
    connection.close()

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        platform_owned_legacy_lilies_db=tmp_path / "missing-lilies.db",
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
        platform_owned_legacy_lilies_db=tmp_path / "missing-lilies.db",
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
        "platform_owned_legacy_lilies",
        "bridge",
        "collaborative_development",
    ]


def test_safe_on_start_requires_complete_ledgers_with_no_auto_consumers(
    tmp_path: Path,
) -> None:
    platform = tmp_path / "platform.db"
    legacy = tmp_path / "legacy.db"
    bridge = tmp_path / "bridge.db"
    development = tmp_path / "development.db"
    _platform_db(platform)
    _lilies_db(legacy)
    _bridge_db(bridge)
    _development_db(development)
    connection = sqlite3.connect(platform)
    connection.execute("DELETE FROM applications")
    connection.execute("DELETE FROM application_versions")
    connection.execute("DELETE FROM durable_jobs")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(legacy)
    connection.execute("UPDATE sessions SET status='closed'")
    connection.execute("UPDATE turns SET status='completed',checkpoint_json='{}'")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(bridge)
    connection.execute("UPDATE local_lilies_assignments SET desired_state='cancelled'")
    connection.commit()
    connection.close()
    connection = sqlite3.connect(development)
    connection.execute("UPDATE collaborative_development_assignments SET status='completed'")
    connection.commit()
    connection.close()

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        platform_owned_legacy_lilies_db=legacy,
        bridge_db=bridge,
        development_db=development,
        process_rows=[],
        model_egress_enabled=True,
    )

    assert result["safety"]["startup_ledger_evidence_complete"] is True
    assert result["safety"]["startup_auto_consumer_count"] == 0
    assert result["safety"]["safe_on_platform_or_daemon_start"] is True


def test_process_detection_and_delta() -> None:
    invocation = "t01h-" + "a" * 32
    runtime_digest = "b" * 64
    workspace_digest = "c" * 64
    binding = (
        f'-c lilies.external_builder_invocation="{invocation}" '
        f'-c lilies.external_builder_runtime_sha256="{runtime_digest}" '
        f'-c lilies.external_builder_workspace_sha256="{workspace_digest}"'
    )
    rows = [
        "123 1 00:10 python -m agent_platform.lilies_cli serve --port 8765",
        "124 1 00:10 node next dev",
        (
            "125 1 00:05 python scripts/run_v04_13_codex_builder.py "
            "--state-root /private/tmp/exp bootstrap --launch-codex"
        ),
        (
            "126 125 00:04 python scripts/run_v04_13_codex_builder_child.py "
            "--handoff /private/tmp/exp/handoff.json"
        ),
        (
            "127 1 00:03 python scripts/run_v04_13_codex_builder.py "
            "--state-root /private/tmp/observe bootstrap"
        ),
        f"128 126 00:02 /opt/codex {binding} exec --json -",
        (
            "129 126 00:01 /usr/bin/sandbox-exec -p (version 1) -- "
            f"/opt/codex {binding} exec --json -"
        ),
        "130 1 00:01 /opt/codex exec --json -",
        (f'131 1 00:01 /opt/codex -c lilies.external_builder_invocation="{invocation}" exec -'),
        f"132 1 00:01 /bin/echo /opt/codex {binding}",
        "133 1 00:01 /usr/bin/sandbox-exec -p '(version 1)' -- /bin/sleep 10",
        (
            "134 1 00:01 /Applications/Codex.app/Contents/Frameworks/"
            "SkyComputerUseClient --event "
            '\'{"type":"turn-ended","prompt":"python '
            "scripts/run_v04_13_codex_builder.py bootstrap --launch-codex\"}'"
        ),
        (
            "135 1 00:01 /Applications/Codex.app/Contents/MacOS/Codex --payload "
            '\'{"type":"turn-ended","prompt":"python '
            "scripts/run_v04_13_enterprise_experiment.py\"}'"
        ),
        (
            "136 1 00:01 python scripts/run_v04_13_codex_builder.py bootstrap "
            "'prior prompt: run_v04_13_codex_builder.py --launch-codex'"
        ),
        (
            "137 1 00:01 /usr/bin/sandbox-exec -p '(version 1)' -- "
            f"/bin/echo /opt/codex {binding} exec --json -"
        ),
        ("138 1 00:01 scripts/run_v04_13_codex_builder.py bootstrap --launch-codex"),
        "139 1 00:01 python -m lilies_agent.cli serve --host 127.0.0.1 --port 8765",
        "140 1 00:01 python -I -m uvicorn agent_platform.api:app --host 127.0.0.1",
        (
            "141 1 00:01 python -c 'import agent_platform.api as api;"
            " import uvicorn; uvicorn.run(api.app)'"
        ),
        "142 1 00:01 python -c 'print(\"agent_platform.api uvicorn\")'",
        (
            "143 1 00:01 python -c from pathlib import Path\\012"
            "import agent_platform.api as api\\012import uvicorn\\012"
            "uvicorn.run(api.app)"
        ),
    ]
    processes = discover_model_capable_processes(rows)
    assert [(item["pid"], item["kind"]) for item in processes] == [
        (123, "local_lilies_daemon"),
        (125, "external_codex_builder"),
        (126, "external_codex_builder"),
        (128, "external_codex_builder"),
        (129, "external_codex_builder"),
        (138, "external_codex_builder"),
        (139, "local_lilies_daemon"),
        (140, "platform_api"),
        (141, "platform_api"),
        (143, "platform_api"),
    ]
    raw = [item for item in processes if item["pid"] in {128, 129}]
    assert all(item["invocation_id"] == invocation for item in raw)
    by_pid = {item["pid"]: item for item in processes}
    assert by_pid[123]["distribution"] == "platform_owned_legacy"
    assert by_pid[139]["distribution"] == "standalone"

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


def test_failed_process_inspection_cannot_report_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        token_monitoring.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="denied",
        ),
    )

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        required_sources=(),
        model_egress_enabled=False,
    )

    assert result["safety"]["process_inspection_complete"] is False
    assert result["safety"]["process_attestation_complete"] is False
    assert result["safety"]["evidence_complete"] is False
    assert result["safety"]["safe_now"] is None


def test_pid_attestation_distinguishes_daemon_from_external_codex(
    tmp_path: Path,
) -> None:
    common = {
        "platform_db": tmp_path / "missing-platform.db",
        "platform_owned_legacy_lilies_db": tmp_path / "missing-lilies.db",
        "bridge_db": tmp_path / "missing-bridge.db",
        "development_db": tmp_path / "missing-development.db",
        "required_sources": (),
        "model_egress_enabled": False,
    }
    daemon = collect_token_monitor_snapshot(
        **common,
        process_rows=["201 1 00:01 python -m lilies_agent.cli serve --host 127.0.0.1 --port 8765"],
    )
    attested_daemon = collect_token_monitor_snapshot(
        **common,
        process_rows=["201 1 00:01 python -m lilies_agent.cli serve --host 127.0.0.1 --port 8765"],
        process_egress_attestations={201: False},
    )
    egressing_daemon = collect_token_monitor_snapshot(
        **common,
        process_rows=["201 1 00:01 python -m lilies_agent.cli serve --host 127.0.0.1 --port 8765"],
        process_egress_attestations={201: True},
    )
    external = collect_token_monitor_snapshot(
        **common,
        process_rows=["202 1 00:01 scripts/run_v04_13_codex_builder.py bootstrap --launch-codex"],
        external_codex_spend_disabled=True,
    )

    assert daemon["safety"]["model_capable_processes_active"] == 1
    assert daemon["safety"]["unblocked_model_processes_active"] == 0
    assert daemon["safety"]["unknown_model_processes_active"] == 1
    assert daemon["safety"]["process_attestation_complete"] is False
    assert daemon["safety"]["safe_now"] is None
    assert daemon["safety"]["background_consumption_observed"] is None
    assert attested_daemon["safety"]["safe_now"] is True
    assert attested_daemon["processes"][0]["safety_status"] == "egress_disabled_attested"
    assert egressing_daemon["safety"]["safe_now"] is False
    assert egressing_daemon["processes"][0]["safety_status"] == "egress_enabled_attested"
    assert external["safety"]["model_capable_processes_active"] == 1
    assert external["safety"]["unblocked_model_processes_active"] == 1
    assert external["safety"]["safe_now"] is False
    assert external["safety"]["background_consumption_observed"] is None
    assert external["safety"]["external_codex_spend_disabled"] is True


def test_standalone_public_usage_is_strict_and_aggregated_by_all_dimensions(
    tmp_path: Path,
) -> None:
    missing = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        required_sources=("standalone_lilies",),
        process_rows=[],
    )
    assert missing["usage"]["totals"]["tokens"] == 0
    assert missing["sources"]["standalone_lilies"]["available"] is False
    assert missing["sources"]["standalone_lilies"]["active_sessions"] is None
    assert missing["safety"]["safe_now"] is None
    assert missing["safety"]["safe_on_platform_or_daemon_start"] is None

    session_id = "a1011039-df1c-4ceb-bca8-8ee50bfe50c4"
    payload = _standalone_usage_payload(
        items=[
            {
                "session_id": session_id,
                "stage": "planning",
                "model": "model-a",
                "recorded_calls": 2,
                "unknown_calls": 1,
                "input_tokens": 100,
                "output_tokens": 23,
                "total_tokens": 123,
                "cost_usd": 0.5,
            }
        ]
    )
    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        standalone_usage_snapshot=payload,
        required_sources=("standalone_lilies",),
        process_rows=[],
    )

    assert result["sources"]["standalone_lilies"]["available"] is True
    assert result["sources"]["standalone_lilies"]["boundary"] == ("authenticated_public_http_only")
    assert result["usage"]["totals"]["tokens"] == 123
    assert result["usage"]["totals"]["model_calls"] == 3
    assert result["usage"]["totals"]["unknown_usage_model_calls"] == 1
    assert result["usage"]["by_stage"][0]["name"] == "planning"
    assert result["usage"]["by_model"][0]["name"] == "model-a"
    assert result["usage"]["by_session"][0]["name"] == session_id
    assert result["safety"]["safe_now"] is None
    assert result["safety"]["safe_on_platform_or_daemon_start"] is None

    incomplete = dict(payload)
    incomplete["total_pages"] = 2
    incomplete["total_items"] = 101
    invalid = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        standalone_usage_snapshot=incomplete,
        required_sources=("standalone_lilies",),
        process_rows=[],
    )
    assert invalid["sources"]["standalone_lilies"]["available"] is False
    assert invalid["sources"]["standalone_lilies"]["schema_valid"] is False
    assert invalid["safety"]["evidence_complete"] is False
    assert invalid["safety"]["safe_now"] is None


def test_standalone_complete_paginated_merge_accepts_more_than_one_page(
    tmp_path: Path,
) -> None:
    items = [
        {
            "session_id": f"00000000-0000-0000-0000-{index + 1:012x}",
            "stage": "execution",
            "model": "model-b",
            "recorded_calls": 1,
            "unknown_calls": 0,
            "input_tokens": index + 1,
            "output_tokens": 1,
            "total_tokens": index + 2,
            "cost_usd": 0.0,
        }
        for index in range(101)
    ]
    payload = {
        **_standalone_usage_payload(items=items),
        "snapshot_kind": "complete_paginated_merge",
        "returned_count": 101,
        "total_items": 101,
        "total_pages": 2,
    }

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        standalone_usage_snapshot=payload,
        required_sources=("standalone_lilies",),
        process_rows=[],
    )

    assert result["sources"]["standalone_lilies"]["available"] is True
    assert result["usage"]["totals"]["model_calls"] == 101
    assert result["usage"]["totals"]["input_tokens"] == sum(range(1, 102))
    assert len(result["usage"]["by_session"]) == 101


def test_standalone_global_observability_is_authoritative_without_double_counting(
    tmp_path: Path,
) -> None:
    platform, bridge, development = _empty_startup_databases(tmp_path)
    session_id = "a1011039-df1c-4ceb-bca8-8ee50bfe50c4"
    acl_usage = _standalone_usage_payload(
        items=[
            {
                "session_id": session_id,
                "stage": "planning",
                "model": "model-a",
                "recorded_calls": 2,
                "unknown_calls": 1,
                "input_tokens": 100,
                "output_tokens": 23,
                "total_tokens": 123,
                "cost_usd": 0.5,
            }
        ]
    )
    envelope = _standalone_observability_envelope(
        usage=acl_usage,
        recorded_calls=3,
        unknown_calls=2,
        input_tokens=150,
        output_tokens=50,
        cost_usd=0.8,
        explicit_resume_candidate_count=2,
    )

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        bridge_db=bridge,
        development_db=development,
        standalone_observability_snapshot=envelope,
        required_sources=("platform", "bridge", "collaborative_development", "standalone_lilies"),
        process_rows=[],
    )

    assert result["usage"]["totals"] == {
        "input_tokens": 150,
        "output_tokens": 50,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "unattributed_tokens": 77,
        "tokens": 200,
        "cost_usd": pytest.approx(0.8),
        "model_calls": 5,
        "usage_records": 3,
        "unknown_usage_model_calls": 2,
    }
    assert result["sources"]["standalone_lilies"]["scope"] == "daemon_global"
    assert result["sources"]["standalone_lilies"]["detail_scope"] == "paired_client_acl"
    assert result["usage"]["by_session"][0]["name"] == session_id
    remainder = next(
        row
        for row in result["usage"]["by_stage"]
        if row["name"] == "standalone_global_unattributed_remainder"
    )
    assert remainder["tokens"] == 77
    assert remainder["model_calls"] == 2
    assert result["safety"]["standalone_model_egress_risk"] is False
    assert result["safety"]["safe_now"] is True
    assert result["safety"]["safe_on_platform_or_daemon_start"] is True
    assert result["safety"]["startup_auto_consumers"][
        "startup_resumable_standalone_lilies_turns"
    ] == 0
    assert result["sources"]["standalone_lilies"]["explicit_resume_candidate_count"] == 2
    assert "daemon-global paired observability bracket" in result["safety"][
        "claim_boundary"
    ]
    assert "daemon-global paired observability totals" in result["usage"]["accounting"][
        "standalone_lilies_granularity"
    ]
    assert compact_token_monitor_snapshot(result)["sources"]["standalone_lilies"] == {
        "active_sessions": 0,
        "startup_resumable_turns": 0,
    }


def test_missing_development_ledger_keeps_complete_idle_receipt_unknown(
    tmp_path: Path,
) -> None:
    platform, bridge, development = _empty_startup_databases(tmp_path)
    development.unlink()

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        bridge_db=bridge,
        development_db=development,
        standalone_observability_snapshot=_standalone_observability_envelope(),
        required_sources=(
            "platform",
            "bridge",
            "collaborative_development",
            "standalone_lilies",
        ),
        process_rows=[],
    )

    assert result["safety"]["missing_required_sources"] == [
        "collaborative_development"
    ]
    assert result["safety"]["safe_now"] is None
    assert result["safety"]["safe_on_platform_or_daemon_start"] is None


def test_active_provider_call_is_explicit_global_unknown_remainder(
    tmp_path: Path,
) -> None:
    platform, bridge, development = _empty_startup_databases(tmp_path)

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        bridge_db=bridge,
        development_db=development,
        standalone_observability_snapshot=_standalone_observability_envelope(
            active_provider_calls=1,
        ),
        required_sources=(
            "platform",
            "bridge",
            "collaborative_development",
            "standalone_lilies",
        ),
        process_rows=[],
    )

    assert result["usage"]["totals"]["model_calls"] == 4
    assert result["usage"]["totals"]["usage_records"] == 2
    assert result["usage"]["totals"]["unknown_usage_model_calls"] == 2
    assert len(result["usage"]["by_stage"]) == 1
    remainder = result["usage"]["by_stage"][0]
    assert remainder["name"] == "standalone_global_unattributed_remainder"
    assert remainder["tokens"] == 123
    assert remainder["cost_usd"] == 0.5
    assert remainder["model_calls"] == 4
    assert remainder["unknown_usage_model_calls"] == 2
    assert result["safety"]["safe_now"] is False


@pytest.mark.parametrize(
    "receipt_changes",
    [
        {"model_egress_enabled": True},
        {"active_sessions": 1},
        {"active_sessions": 1, "active_model_turns": 1},
        {
            "active_sessions": 1,
            "active_model_turns": 1,
            "active_provider_calls": 1,
        },
        {
            "active_sessions": 1,
            "active_model_turns": 1,
            "active_provider_calls": 1,
            "active_development_model_calls": 1,
        },
    ],
)
def test_standalone_runtime_or_egress_risk_forces_safe_now_false(
    tmp_path: Path,
    receipt_changes: dict[str, object],
) -> None:
    platform, bridge, development = _empty_startup_databases(tmp_path)
    envelope = _standalone_observability_envelope(**receipt_changes)

    result = collect_token_monitor_snapshot(
        platform_db=platform,
        bridge_db=bridge,
        development_db=development,
        standalone_observability_snapshot=envelope,
        required_sources=("platform", "bridge", "collaborative_development", "standalone_lilies"),
        process_rows=[],
    )

    assert result["safety"]["standalone_model_egress_risk"] is True
    assert result["safety"]["safe_now"] is False


def test_standalone_startup_counts_only_automatic_resume_candidates(tmp_path: Path) -> None:
    platform, bridge, development = _empty_startup_databases(tmp_path)
    common = {
        "platform_db": platform,
        "bridge_db": bridge,
        "development_db": development,
        "required_sources": (
            "platform",
            "bridge",
            "collaborative_development",
            "standalone_lilies",
        ),
        "process_rows": [],
    }
    explicit_only = collect_token_monitor_snapshot(
        **common,
        standalone_observability_snapshot=_standalone_observability_envelope(
            explicit_resume_candidate_count=3,
        ),
    )
    automatic = collect_token_monitor_snapshot(
        **common,
        standalone_observability_snapshot=_standalone_observability_envelope(
            automatic_model_resume_count=1,
            explicit_resume_candidate_count=3,
        ),
    )

    assert explicit_only["safety"]["safe_on_platform_or_daemon_start"] is True
    assert automatic["safety"]["safe_on_platform_or_daemon_start"] is False
    assert automatic["safety"]["startup_auto_consumer_count"] == 1


@pytest.mark.parametrize(
    ("section", "field", "invalid"),
    [
        ("runtime", "active_sessions", True),
        ("runtime", "active_provider_calls", "1"),
        ("runtime", "active_sessions", 9_223_372_036_854_775_808),
        ("runtime", "active_model_turns", 1),
        ("runtime", "active_development_model_calls", 2),
        ("usage", "ledger_cursor", 0),
        ("usage", "attempted_calls", 0),
        ("usage", "total_tokens", 0),
        pytest.param(
            "usage",
            "cost_usd",
            10**10_000,
            id="oversized-cost",
        ),
    ],
)
def test_standalone_observability_rejects_wire_types_hierarchy_and_cursor(
    tmp_path: Path,
    section: str,
    field: str,
    invalid: object,
) -> None:
    envelope = _standalone_observability_envelope()
    for edge in ("before", "after"):
        receipt = envelope[edge]
        assert isinstance(receipt, dict)
        nested = receipt[section]
        assert isinstance(nested, dict)
        nested[field] = invalid

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        standalone_observability_snapshot=envelope,
        required_sources=("standalone_lilies",),
        process_rows=[],
    )

    assert result["sources"]["standalone_lilies"]["available"] is False
    assert result["safety"]["safe_now"] is None
    assert result["safety"]["safe_on_platform_or_daemon_start"] is None


def test_standalone_monitor_rejects_non_object_envelope() -> None:
    result = token_monitoring._standalone_lilies_observability_snapshot([])

    assert result["available"] is False
    assert result["schema_valid"] is False
    assert result["schema_errors"] == ["observability_envelope_type"]


@pytest.mark.parametrize("mutation", ["self_reported_stable", "usage_counter_drift"])
def test_standalone_monitor_recomputes_bracket_stability(
    tmp_path: Path,
    mutation: str,
) -> None:
    envelope = _standalone_observability_envelope()
    if mutation == "self_reported_stable":
        envelope["stable"] = True
    else:
        after = envelope["after"]
        assert isinstance(after, dict)
        usage = after["usage"]
        assert isinstance(usage, dict)
        usage["attempted_calls"] = int(usage["attempted_calls"]) + 1
        usage["recorded_calls"] = int(usage["recorded_calls"]) + 1

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        standalone_observability_snapshot=envelope,
        required_sources=("standalone_lilies",),
        process_rows=[],
    )

    source = result["sources"]["standalone_lilies"]
    assert source["available"] is False
    assert source["schema_valid"] is False
    assert result["safety"]["safe_now"] is None


def test_standalone_acl_cannot_exceed_global_or_turn_missing_receipt_into_zero(
    tmp_path: Path,
) -> None:
    acl_usage = _standalone_usage_payload(
        items=[
            {
                "session_id": "a1011039-df1c-4ceb-bca8-8ee50bfe50c4",
                "stage": "planning",
                "model": "model-a",
                "recorded_calls": 2,
                "unknown_calls": 1,
                "input_tokens": 100,
                "output_tokens": 23,
                "total_tokens": 123,
                "cost_usd": 0.5,
            }
        ]
    )
    invalid = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        standalone_observability_snapshot=_standalone_observability_envelope(
            usage=acl_usage,
            recorded_calls=1,
            unknown_calls=0,
            input_tokens=10,
            output_tokens=1,
            cost_usd=0.1,
        ),
        required_sources=("standalone_lilies",),
        process_rows=[],
    )
    missing = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        required_sources=("standalone_lilies",),
        process_rows=[],
    )

    assert invalid["sources"]["standalone_lilies"]["available"] is False
    assert invalid["usage"]["totals"]["tokens"] == 0
    assert invalid["safety"]["safe_now"] is None
    assert missing["sources"]["standalone_lilies"]["active_sessions"] is None
    assert missing["safety"]["standalone_model_egress_risk"] is None
    assert missing["safety"]["safe_now"] is None


@pytest.mark.parametrize("egress_enabled", [False, True])
def test_standalone_health_attestation_is_bound_to_discovered_pid(
    monkeypatch: pytest.MonkeyPatch,
    egress_enabled: bool,
) -> None:
    async def discover(
        _path: Path,
        _client: object,
    ) -> dict[str, object]:
        return {
            "status": "available",
            "pid": 501,
            "base_url": "http://127.0.0.1:8765",
            "model_egress_enabled": egress_enabled,
        }

    monkeypatch.setattr(token_monitor_cli, "discover_local_lilies", discover)
    monkeypatch.setattr(
        token_monitor_cli,
        "_listener_matches_process",
        lambda pid, base_url: pid == 501 and base_url.endswith(":8765"),
    )
    processes = [
        {
            "pid": 501,
            "kind": "local_lilies_daemon",
            "distribution": "standalone",
        }
    ]

    assert token_monitor_cli._standalone_daemon_attestations(
        processes,
        discovery_path=Path("/private/discovery/daemon.json"),
    ) == {501: egress_enabled}

    processes[0]["pid"] = 502
    assert (
        token_monitor_cli._standalone_daemon_attestations(
            processes,
            discovery_path=Path("/private/discovery/daemon.json"),
        )
        == {}
    )


def test_listener_attestation_rejects_pid_or_port_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lsof = tmp_path / "lsof"
    lsof.write_text("test", encoding="utf-8")
    output = {"value": "p601\nn127.0.0.1:8765\n"}

    def run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout=output["value"], stderr="")

    monkeypatch.setattr(token_monitor_cli.subprocess, "run", run)
    assert token_monitor_cli._listener_matches_process(
        601,
        "http://127.0.0.1:8765",
        lsof_path=lsof,
    )

    output["value"] = "p601\nn127.0.0.1:9999\n"
    assert not token_monitor_cli._listener_matches_process(
        601,
        "http://127.0.0.1:8765",
        lsof_path=lsof,
    )
    output["value"] = "p602\nn127.0.0.1:8765\n"
    assert not token_monitor_cli._listener_matches_process(
        601,
        "http://127.0.0.1:8765",
        lsof_path=lsof,
    )


def test_compact_snapshot_and_summary_json_cli_exclude_verbose_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    platform = tmp_path / "platform.db"
    lilies = tmp_path / "lilies.db"
    bridge = tmp_path / "bridge.db"
    development = tmp_path / "development.db"
    _platform_db(platform)
    _lilies_db(lilies)
    _bridge_db(bridge)
    _development_db(development)
    snapshot = collect_token_monitor_snapshot(
        platform_db=platform,
        platform_owned_legacy_lilies_db=lilies,
        bridge_db=bridge,
        development_db=development,
        process_rows=[],
        generated_at="2026-07-25T01:00:00+00:00",
    )
    summary = compact_token_monitor_snapshot(snapshot)

    assert set(summary) == {
        "generated_at",
        "delta",
        "safety",
        "usage",
        "processes",
        "sources",
    }
    assert summary["delta"] is None
    assert set(summary["usage"]) == {"totals", "by_stage", "by_model", "by_session"}
    assert summary["sources"] == {
        "platform": {
            "active_tasks": 0,
            "published_schedule_nodes": 1,
            "due_durable_jobs": 1,
        },
        "platform_owned_legacy_lilies": {
            "active_sessions": 1,
            "startup_resumable_turns": 1,
        },
        "standalone_lilies": {
            "active_sessions": None,
            "startup_resumable_turns": None,
        },
        "bridge": {"recoverable_assignments": 1},
        "collaborative_development": {
            "active_assignments": 1,
            "reserved_provider_costs": 0,
        },
    }
    encoded = json.dumps(summary)
    assert '"samples"' not in encoded
    assert '"sessions"' not in encoded
    assert '"path"' not in encoded
    assert '"accounting"' not in encoded
    assert compact_token_monitor_snapshot(
        snapshot,
        delta={"tokens": 0, "tokens_per_minute": 0.0},
    )["delta"] == {"tokens": 0, "tokens_per_minute": 0.0}
    parser_defaults = token_monitor_cli.build_parser().parse_args([])
    assert parser_defaults.platform_owned_legacy_lilies_db is None
    assert not hasattr(parser_defaults, "lilies_db")

    monkeypatch.setattr(
        token_monitor_cli,
        "collect_token_monitor_snapshot",
        lambda **_kwargs: snapshot,
    )
    monkeypatch.setattr(token_monitor_cli, "_model_egress_enabled", lambda: False)
    monkeypatch.setattr(token_monitor_cli, "discover_model_capable_processes", lambda: [])
    assert token_monitor_cli.main(["--summary-json"]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == summary

    captured_paths: dict[str, object] = {}

    def collect_with_paths(**kwargs: object) -> dict[str, object]:
        captured_paths.update(kwargs)
        return snapshot

    state_root = tmp_path / "experiment"
    legacy = tmp_path / "platform-owned-legacy-lilies.db"
    monkeypatch.setattr(
        token_monitor_cli,
        "collect_token_monitor_snapshot",
        collect_with_paths,
    )
    assert (
        token_monitor_cli.main(
            [
                "--state-root",
                str(state_root),
                "--summary-json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert captured_paths["platform_owned_legacy_lilies_db"] is None

    captured_paths.clear()
    assert (
        token_monitor_cli.main(
            [
                "--state-root",
                str(state_root),
                "--platform-owned-legacy-lilies-db",
                str(legacy),
                "--summary-json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert captured_paths["platform_db"] == (state_root / "platform-data" / "agent_platform.db")
    assert captured_paths["bridge_db"] == (state_root / "platform-data" / "local-lilies-bridge.db")
    assert captured_paths["development_db"] == (
        state_root / "platform-data" / "collaborative-development.db"
    )
    assert captured_paths["platform_owned_legacy_lilies_db"] == legacy
    assert captured_paths["standalone_usage_snapshot"] is None
    assert captured_paths["standalone_observability_snapshot"] is None
    assert captured_paths["external_codex_spend_disabled"] is False

    sentinel = state_root / "EXTERNAL_CODEX_SPEND_DISABLED"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("state=disabled\n", encoding="utf-8")
    captured_paths.clear()
    assert (
        token_monitor_cli.main(
            [
                "--state-root",
                str(state_root),
                "--platform-owned-legacy-lilies-db",
                str(legacy),
                "--summary-json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert captured_paths["external_codex_spend_disabled"] is True


def test_cli_rejects_every_sqlite_source_inside_or_aliasing_standalone_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standalone_state = tmp_path / "standalone-state"
    standalone_state.mkdir()
    discovery = standalone_state / "daemon.json"
    daemon_db = standalone_state / "lilies.db"
    daemon_db.write_bytes(b"private standalone state")
    alias = tmp_path / "legacy-alias.db"
    alias.hardlink_to(daemon_db)
    monkeypatch.setattr(
        token_monitor_cli,
        "collect_token_monitor_snapshot",
        lambda **_kwargs: pytest.fail("unsafe SQLite path reached the collector"),
    )

    for option in (
        "--platform-db",
        "--bridge-db",
        "--development-db",
        "--platform-owned-legacy-lilies-db",
    ):
        for unsafe_path in (daemon_db, standalone_state / "renamed.db", alias):
            with pytest.raises(SystemExit) as raised:
                token_monitor_cli.main(
                    [
                        "--standalone-discovery-record",
                        str(discovery),
                        option,
                        str(unsafe_path),
                        "--summary-json",
                    ]
                )
            assert raised.value.code == 2


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
        platform_owned_legacy_lilies_db=lilies,
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        required_sources=("platform_owned_legacy_lilies",),
        process_rows=[],
    )

    totals = result["usage"]["totals"]
    assert totals["model_calls"] == 57
    assert totals["unknown_usage_model_calls"] == 2
    assert totals["tokens"] == 7_837_176
    assert result["sources"]["platform_owned_legacy_lilies"]["active_sessions"] == [
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
    connection.execute("UPDATE sessions SET token_count=25,model_call_count=2 WHERE id='session-1'")
    connection.execute(
        "UPDATE turns SET model_call_count=2,checkpoint_json=? WHERE id='turn-1'",
        (json.dumps({"metrics": {}, "pending": {}}),),
    )
    connection.commit()
    connection.close()

    result = collect_token_monitor_snapshot(
        platform_db=tmp_path / "missing-platform.db",
        platform_owned_legacy_lilies_db=lilies,
        bridge_db=tmp_path / "missing-bridge.db",
        development_db=tmp_path / "missing-development.db",
        process_rows=[],
    )

    totals = result["usage"]["totals"]
    assert totals["model_calls"] == 2
    assert totals["usage_records"] == 1
    assert totals["unknown_usage_model_calls"] == 2
    assert totals["tokens"] == 25
