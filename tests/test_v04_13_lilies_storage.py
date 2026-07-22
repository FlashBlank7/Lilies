from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_platform.lilies_storage import (
    LiliesAccessDeniedError,
    LiliesAuthenticationError,
    LiliesConflictError,
    LiliesStorage,
)


async def _paired_client(
    storage: LiliesStorage,
    *,
    name: str,
    scopes: list[str],
    nonce: str,
) -> dict[str, object]:
    pairing = await storage.create_pairing_code(allowed_scopes=scopes)
    return await storage.exchange_pairing_code(
        pairing["pairing_code"],
        name,
        scopes,
        nonce,
        "sha256:" + "a" * 64,
    )


def _digest_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_independent_schema_permissions_and_transactional_session_events(tmp_path) -> None:
    data_dir = tmp_path / "lilies-home"
    storage = LiliesStorage(data_dir)

    assert await storage.initialize() == {
        "schema_version": 3,
        "interrupted_sessions": 0,
        "interrupted_turns": 0,
    }
    assert data_dir.stat().st_mode & 0o777 == 0o700
    assert storage.db_path.stat().st_mode & 0o777 == 0o600

    with sqlite3.connect(storage.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert version == 3
    assert journal_mode == "wal"
    assert {
        "sessions",
        "messages",
        "turns",
        "events",
        "pairing_codes",
        "clients",
        "client_session_acl",
        "reader_acks",
        "permission_requests",
        "credentials",
        "security_events",
    } <= tables

    session = await storage.create_session(
        agent_version="agent-7",
        model_profile="bounded",
        system_identity_version="identity-3",
        config={"max_turns": 12},
        profile={"provider": "test"},
        assignment_id="assignment-1",
        assignment={"assignment_id": "assignment-1"},
        platform_contract_digest="sha256:" + "b" * 64,
    )
    updated = await storage.update_session_context(
        session["id"],
        context_summary="business goal preserved",
        summary_through_event_seq=1,
        last_platform_cursor=4,
    )
    assert updated["config"] == {"max_turns": 12}
    assert updated["profile"] == {"provider": "test"}
    assert updated["context_summary"] == "business goal preserved"
    assert updated["summary_through_event_seq"] == 1

    message = await storage.add_message(session["id"], "user", {"text": "continue"})
    turn = await storage.create_turn(
        session["id"],
        "request-1",
        "idempotency-key-0001",
        input_message_id=message["id"],
    )
    await storage.finish_turn(
        turn["id"],
        "completed",
        token_count=23,
        cost_usd=0.04,
        tool_count=2,
        model_call_count=1,
    )
    finished = await storage.get_session(session["id"])
    assert finished["status"] == "ready"
    assert (finished["token_count"], finished["tool_count"], finished["model_call_count"]) == (
        23,
        2,
        1,
    )
    events = await storage.list_events(session["id"])
    status_events = [event for event in events if event["event_type"] == "session.status_changed"]
    assert [(event["data"]["from_status"], event["data"]["to_status"]) for event in status_events] == [
        ("ready", "running"),
        ("running", "ready"),
    ]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_pairing_is_scoped_one_use_hashed_replay_safe_and_revocable(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    code = await storage.create_pairing_code(allowed_scopes=["lilies.session:read"])
    assert set(code) == {"code_id", "pairing_code", "allowed_scopes", "expires_at"}
    assert code["pairing_code"].replace("-", "").isalnum()

    exchanged = await storage.exchange_pairing_code(
        code["pairing_code"],
        "platform",
        ["lilies.session:read", "lilies.session:write"],
        "nonce_for_pairing_000001",
        "sha256:" + "c" * 64,
    )
    assert exchanged["granted_scopes"] == ["lilies.session:read"]
    authenticated = await storage.authenticate_client(
        exchanged["access_token"], required_scope="lilies.session:read"
    )
    assert authenticated["client_id"] == exchanged["client_id"]
    assert "token_digest" not in authenticated

    database_bytes = storage.db_path.read_bytes()
    assert exchanged["access_token"].encode() not in database_bytes
    assert code["pairing_code"].encode() not in database_bytes
    with pytest.raises(LiliesAuthenticationError):
        await storage.exchange_pairing_code(
            code["pairing_code"],
            "cli:test",
            ["lilies.session:read"],
            "another_nonce_0000000001",
            "sha256:" + "c" * 64,
        )

    second = await storage.create_pairing_code(allowed_scopes=["lilies.session:read"])
    with pytest.raises(LiliesAuthenticationError, match="nonce"):
        await storage.exchange_pairing_code(
            second["pairing_code"],
            "cli:test",
            ["lilies.session:read"],
            "nonce_for_pairing_000001",
            "sha256:" + "c" * 64,
        )
    with pytest.raises(LiliesAccessDeniedError, match="unknown"):
        await storage.exchange_pairing_code(
            second["pairing_code"],
            "cli:test",
            ["not.a.real:scope"],
            "unique_nonce_for_unknown_scope",
            "sha256:" + "c" * 64,
        )

    await storage.revoke_client(str(exchanged["client_id"]))
    with pytest.raises(LiliesAuthenticationError, match="revoked"):
        await storage.authenticate_client(str(exchanged["access_token"]))
    rejection_reasons = {
        event["details"].get("reason")
        for event in await storage.list_security_events()
        if event["event_type"] == "pairing.exchange_rejected"
    }
    assert {"code_redeemed", "nonce_replay", "unknown_scope"} <= rejection_reasons


@pytest.mark.asyncio
async def test_pairing_expiry_is_rejected_and_recorded(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    code = await storage.create_pairing_code(allowed_scopes=["lilies.session:read"])
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute("UPDATE pairing_codes SET expires_at=? WHERE id=?", (expired, code["code_id"]))

    with pytest.raises(LiliesAuthenticationError, match="expired"):
        await storage.exchange_pairing_code(
            code["pairing_code"],
            "platform",
            ["lilies.session:read"],
            "expiry_nonce_00000000001",
            "sha256:" + "d" * 64,
        )
    events = await storage.list_security_events()
    assert any(event["details"].get("reason") == "code_expired" for event in events)


@pytest.mark.asyncio
async def test_expired_client_rotates_in_place_only_with_exact_old_proof(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    scopes = ["lilies.session:read", "lilies.session:write"]
    original = await _paired_client(
        storage,
        name="cli:rotation",
        scopes=scopes,
        nonce="rotation_original_nonce_001",
    )
    session = await storage.create_session(client_id=str(original["client_id"]))
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute(
            "UPDATE clients SET expires_at=? WHERE id=?",
            (expired, original["client_id"]),
        )
    with pytest.raises(LiliesAuthenticationError, match="expired"):
        await storage.authenticate_client(str(original["access_token"]))

    rotation_code = await storage.create_pairing_code(allowed_scopes=scopes)
    rotated = await storage.exchange_pairing_code(
        rotation_code["pairing_code"],
        "cli:rotation",
        scopes,
        "rotation_repair_nonce_00001",
        "sha256:" + "a" * 64,
        previous_client_id=str(original["client_id"]),
        previous_access_token=str(original["access_token"]),
    )
    assert rotated["client_id"] == original["client_id"]
    assert rotated["access_token"] != original["access_token"]
    assert [
        item["id"]
        for item in await storage.list_sessions(client_id=str(rotated["client_id"]))
    ] == [session["id"]]
    await storage.authenticate_client(str(rotated["access_token"]))
    with pytest.raises(LiliesAuthenticationError):
        await storage.authenticate_client(str(original["access_token"]))

    rejected_code = await storage.create_pairing_code(allowed_scopes=scopes)
    wrong_previous_token = f"{original['client_id']}." + "wrong" * 10
    with pytest.raises(LiliesAuthenticationError, match="previous client proof"):
        await storage.exchange_pairing_code(
            rejected_code["pairing_code"],
            "cli:rotation",
            scopes,
            "rotation_wrong_nonce_00001",
            "sha256:" + "a" * 64,
            previous_client_id=str(original["client_id"]),
            previous_access_token=wrong_previous_token,
        )
    fresh_same_name = await storage.exchange_pairing_code(
        rejected_code["pairing_code"],
        "cli:rotation",
        scopes,
        "rotation_fresh_nonce_00001",
        "sha256:" + "a" * 64,
    )
    assert fresh_same_name["client_id"] != original["client_id"]
    assert await storage.list_sessions(client_id=str(fresh_same_name["client_id"])) == []

    await storage.revoke_client(str(original["client_id"]))
    revoked_code = await storage.create_pairing_code(allowed_scopes=scopes)
    with pytest.raises(LiliesAuthenticationError, match="previous client proof"):
        await storage.exchange_pairing_code(
            revoked_code["pairing_code"],
            "cli:rotation",
            scopes,
            "rotation_revoked_nonce_001",
            "sha256:" + "a" * 64,
            previous_client_id=str(original["client_id"]),
            previous_access_token=str(rotated["access_token"]),
        )

    security_json = json.dumps(await storage.list_security_events())
    assert str(original["access_token"]) not in security_json
    assert str(rotated["access_token"]) not in security_json
    assert wrong_previous_token not in security_json


@pytest.mark.asyncio
async def test_session_acl_event_replay_and_monotonic_reader_ack(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    first = await _paired_client(
        storage,
        name="cli:first",
        scopes=["lilies.session:read", "lilies.session:write"],
        nonce="first_client_nonce_000001",
    )
    second = await _paired_client(
        storage,
        name="cli:second",
        scopes=["lilies.session:read"],
        nonce="second_client_nonce_00001",
    )
    session = await storage.create_session(client_id=str(first["client_id"]))
    event = await storage.append_event(session["id"], "assistant.delta", {"text": "hello"})

    assert [item["id"] for item in await storage.list_sessions(client_id=str(first["client_id"]))] == [
        session["id"]
    ]
    assert await storage.list_sessions(client_id=str(second["client_id"])) == []
    with pytest.raises(LiliesAccessDeniedError):
        await storage.list_events(session["id"], client_id=str(second["client_id"]))

    await storage.grant_session_access(str(second["client_id"]), session["id"])
    replay = await storage.list_events(
        session["id"], after=1, client_id=str(second["client_id"])
    )
    assert replay == [event]
    ack = await storage.ack_events(str(second["client_id"]), session["id"], event["seq"])
    assert ack["cursor"] == event["seq"]
    older = await storage.ack_events(str(second["client_id"]), session["id"], 1)
    assert older["cursor"] == event["seq"]
    with pytest.raises(LiliesConflictError):
        await storage.ack_events(str(second["client_id"]), session["id"], 999)


@pytest.mark.asyncio
async def test_permission_wait_survives_restart_and_decision_is_idempotent(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    resolver = await _paired_client(
        storage,
        name="cli:resolver",
        scopes=["lilies.session:read", "lilies.permission:resolve"],
        nonce="permission_nonce_0000001",
    )
    session = await storage.create_session(client_id=str(resolver["client_id"]))
    turn = await storage.create_turn(session["id"], "request-p", "permission-idempotency-1")
    secret = "PRIVATE_TOOL_INPUT_MUST_NOT_LEAK"
    original_input = {"authorization": secret, "record": 4}
    original_digest = _digest_json(original_input)
    with pytest.raises(LiliesConflictError, match="canonical tool input"):
        await storage.create_permission_request(
            session["id"],
            turn["id"],
            "connector.write",
            "sha256:" + "e" * 64,
            tool_call_id="call-mismatch",
            tool_input=original_input,
        )
    permission = await storage.create_permission_request(
        session["id"],
        turn["id"],
        "connector.write",
        original_digest,
        request_id="11111111-1111-4111-8111-111111111111",
        tool_call_id="call-99",
        tool_input=original_input,
        checkpoint={"phase": "before_side_effect", "request_id": "write-4"},
        input_summary={"authorization": "[REDACTED]", "record": 4},
    )
    assert permission["tool_input"]["authorization"] == secret
    assert (await storage.get_session(session["id"]))["status"] == "waiting_permission"

    restarted = LiliesStorage(storage.data_dir)
    recovery = await restarted.initialize()
    assert recovery["interrupted_sessions"] == 0
    assert recovery["interrupted_turns"] == 0
    pending = (await restarted.list_pending_permission_requests(session_id=session["id"]))[0]
    assert pending["id"] == permission["id"]
    assert pending["tool_call_id"] == "call-99"
    assert pending["tool_input"]["authorization"] == secret
    assert pending["checkpoint"]["phase"] == "before_side_effect"

    events_json = json.dumps(await restarted.list_events(session["id"]), ensure_ascii=False)
    assert secret not in events_json
    decision = await restarted.resolve_permission_request(
        session["id"],
        permission["id"],
        "allow",
        client_id=str(resolver["client_id"]),
        expected_input_digest=original_digest,
        updated_input={"record": 5},
        message="approved with correction",
        idempotency_key="permission-decision-0001",
    )
    assert decision["status"] == "allowed"
    assert decision["tool_input"] == original_input
    assert decision["decision_input"] == {"record": 5}
    assert decision["original_input_digest"] == original_digest
    assert decision["approved_input_digest"] == _digest_json({"record": 5})
    assert decision["replayed"] is False
    replay = await restarted.resolve_permission_request(
        session["id"],
        permission["id"],
        "allow",
        client_id=str(resolver["client_id"]),
        expected_input_digest=original_digest,
        updated_input={"record": 5},
        message="approved with correction",
        idempotency_key="permission-decision-0001",
    )
    assert replay["replayed"] is True
    with pytest.raises(LiliesConflictError):
        await restarted.resolve_permission_request(
            session["id"],
            permission["id"],
            "allow",
            expected_input_digest=original_digest,
            updated_input={"record": 6},
            idempotency_key="permission-decision-0001",
        )


@pytest.mark.asyncio
async def test_running_recovery_interrupts_without_replaying_checkpoint(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    session = await storage.create_session()
    turn = await storage.create_turn(session["id"], "request-r", "recovery-idempotency-1")
    await storage.update_turn_checkpoint(
        turn["id"],
        phase="connector_write_uncertain",
        checkpoint={
            "receipt_query_required": True,
            "metrics": {
                "usage": {"input_tokens": 11, "output_tokens": 4, "cost_usd": 0.25},
                "tool_calls": 3,
                "model_calls": 2,
            },
        },
        side_effect_state="unknown",
    )

    restarted = LiliesStorage(storage.data_dir)
    assert await restarted.initialize() == {
        "schema_version": 3,
        "interrupted_sessions": 1,
        "interrupted_turns": 1,
    }
    recovered_turn = await restarted.get_turn(turn["id"])
    assert recovered_turn["status"] == "interrupted"
    assert recovered_turn["side_effect_state"] == "unknown"
    assert recovered_turn["checkpoint"]["receipt_query_required"] is True
    assert recovered_turn["token_count"] == 15
    assert recovered_turn["cost_usd"] == 0.25
    assert recovered_turn["tool_count"] == 3
    assert recovered_turn["model_call_count"] == 2
    assert recovered_turn["metrics_settled_at"] is not None
    recovered_session = await restarted.get_session(session["id"])
    assert recovered_session["status"] == "interrupted"
    assert recovered_session["token_count"] == 15
    assert recovered_session["cost_usd"] == 0.25
    assert recovered_session["tool_count"] == 3
    assert recovered_session["model_call_count"] == 2
    assert await restarted.initialize() == {
        "schema_version": 3,
        "interrupted_sessions": 0,
        "interrupted_turns": 0,
    }
    settled_again = await restarted.get_session(session["id"])
    assert settled_again["token_count"] == 15
    assert settled_again["cost_usd"] == 0.25
    replay = await restarted.create_turn(
        session["id"], "request-r", "recovery-idempotency-1"
    )
    assert replay["id"] == turn["id"]
    assert replay["replayed"] is True
    assert replay["status"] == "interrupted"
    with pytest.raises(LiliesConflictError, match="cannot start a turn from interrupted"):
        await restarted.create_turn(
            session["id"],
            "request-without-explicit-resume",
            "recovery-idempotency-2",
        )
    resumed = await restarted.create_resume_turn(
        session["id"],
        "explicit-resume-request",
        "explicit-resume-idempotency-1",
    )
    assert resumed["status"] == "running"
    assert resumed["phase"] == "resume"
    assert [
        event["event_type"] for event in await restarted.list_events(session["id"])
    ].count("session.interrupted") == 1


@pytest.mark.asyncio
async def test_cancel_active_turn_atomically_settles_waiting_permission_checkpoint_once(
    tmp_path,
) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    session = await storage.create_session()
    turn = await storage.create_turn(
        session["id"],
        "cancel-request",
        "cancel-turn-idempotency",
    )
    await storage.update_turn_checkpoint(
        turn["id"],
        phase="before_permission",
        checkpoint={
            "metrics": {
                "usage": {"input_tokens": 8, "output_tokens": 2, "cost_usd": 0.03},
                "tool_calls": 2,
                "model_calls": 1,
            }
        },
    )
    tool_input = {"path": "result.txt", "content": "do not execute"}
    permission = await storage.create_permission_request(
        session["id"],
        turn["id"],
        "workspace_write",
        _digest_json(tool_input),
        tool_call_id="cancel-tool-call",
        tool_input=tool_input,
    )

    cancelled = await storage.cancel_active_turn(turn["id"], reason="user_cancel")

    assert cancelled["replayed"] is False
    assert cancelled["cancelled_permission_id"] == permission["id"]
    assert cancelled["turn"]["status"] == "cancelled"
    assert cancelled["turn"]["token_count"] == 10
    assert cancelled["turn"]["cost_usd"] == 0.03
    assert cancelled["turn"]["tool_count"] == 2
    assert cancelled["turn"]["model_call_count"] == 1
    assert cancelled["turn"]["metrics_settled_at"] is not None
    assert cancelled["session"]["status"] == "cancelled"
    assert cancelled["session"]["token_count"] == 10
    assert cancelled["session"]["cost_usd"] == 0.03
    assert cancelled["session"]["tool_count"] == 2
    assert cancelled["session"]["model_call_count"] == 1
    assert cancelled["session"]["waiting_permission_id"] is None
    assert cancelled["session"]["waiting_collaboration_id"] is None
    assert (await storage.get_permission_request(permission["id"]))["status"] == "cancelled"

    replay = await storage.cancel_active_turn(turn["id"], reason="user_cancel")
    assert replay["replayed"] is True
    assert replay["cancelled_permission_id"] == permission["id"]
    assert replay["session"]["token_count"] == 10
    assert replay["session"]["cost_usd"] == 0.03
    assert replay["session"]["tool_count"] == 2
    assert replay["session"]["model_call_count"] == 1


@pytest.mark.asyncio
async def test_daemon_stop_atomically_cancels_waiting_permission_and_keeps_session_resumable(
    tmp_path,
) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    session = await storage.create_session()
    turn = await storage.create_turn(session["id"], "stop-request", "stop-turn-idempotency")
    tool_input = {"path": "result.txt", "content": "do not execute"}
    permission = await storage.create_permission_request(
        session["id"],
        turn["id"],
        "workspace_write",
        _digest_json(tool_input),
        tool_call_id="stop-tool-call",
        tool_input=tool_input,
    )

    stopped = await storage.cancel_turn_for_stop(
        turn["id"],
        reason="daemon_stop",
        token_count=7,
        cost_usd=0.01,
        tool_count=1,
        model_call_count=2,
    )
    assert stopped["replayed"] is False
    assert stopped["cancelled_permission_id"] == permission["id"]
    assert stopped["turn"]["status"] == "cancelled"
    assert stopped["turn"]["token_count"] == 7
    assert stopped["session"]["status"] == "interrupted"
    assert stopped["session"]["waiting_permission_id"] is None
    assert stopped["session"]["waiting_collaboration_id"] is None
    assert stopped["session"]["token_count"] == 7
    assert (await storage.get_permission_request(permission["id"]))["status"] == "cancelled"

    replay = await storage.cancel_turn_for_stop(turn["id"], reason="daemon_stop")
    assert replay["replayed"] is True
    assert replay["cancelled_permission_id"] == permission["id"]
    assert replay["session"]["token_count"] == 7

    collaboration_session = await storage.create_session()
    collaboration_turn = await storage.create_turn(
        collaboration_session["id"],
        "collaboration-stop-request",
        "collaboration-stop-idempotency",
    )
    await storage.transition_session(
        collaboration_session["id"],
        "waiting_collaboration",
        expected_status="running",
    )
    await storage.update_session_context(
        collaboration_session["id"],
        waiting_collaboration_id="report-1",
    )
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute(
            "UPDATE turns SET status='waiting_collaboration' WHERE id=?",
            (collaboration_turn["id"],),
        )
    collaboration_stop = await storage.cancel_turn_for_stop(
        collaboration_turn["id"], reason="daemon_stop"
    )
    assert collaboration_stop["turn"]["status"] == "cancelled"
    assert collaboration_stop["session"]["status"] == "interrupted"
    assert collaboration_stop["session"]["waiting_collaboration_id"] is None


@pytest.mark.asyncio
async def test_cancel_active_turn_settles_waiting_collaboration_without_runtime_task(
    tmp_path,
) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    session = await storage.create_session()
    turn = await storage.create_turn(
        session["id"],
        "collaboration-cancel-request",
        "collaboration-cancel-idempotency",
    )
    await storage.update_turn_checkpoint(
        turn["id"],
        phase="waiting_for_developer",
        checkpoint={
            "metrics": {
                "usage": {"input_tokens": 5, "output_tokens": 1, "cost_usd": 0.02},
                "tool_calls": 1,
                "model_calls": 2,
            }
        },
    )
    await storage.transition_session(
        session["id"],
        "waiting_collaboration",
        expected_status="running",
    )
    await storage.update_session_context(
        session["id"],
        waiting_collaboration_id="report-without-task",
    )
    with sqlite3.connect(storage.db_path) as conn:
        conn.execute(
            "UPDATE turns SET status='waiting_collaboration' WHERE id=?",
            (turn["id"],),
        )

    cancelled = await storage.cancel_active_turn(turn["id"], reason="user_cancel")

    assert cancelled["replayed"] is False
    assert cancelled["turn"]["status"] == "cancelled"
    assert cancelled["turn"]["token_count"] == 6
    assert cancelled["turn"]["cost_usd"] == 0.02
    assert cancelled["turn"]["tool_count"] == 1
    assert cancelled["turn"]["model_call_count"] == 2
    assert cancelled["turn"]["metrics_settled_at"] is not None
    assert cancelled["session"]["status"] == "cancelled"
    assert cancelled["session"]["waiting_collaboration_id"] is None
    assert cancelled["session"]["token_count"] == 6
    assert cancelled["session"]["cost_usd"] == 0.02
    assert cancelled["session"]["tool_count"] == 1
    assert cancelled["session"]["model_call_count"] == 2

    replay = await storage.cancel_active_turn(turn["id"], reason="user_cancel")
    assert replay["replayed"] is True
    assert replay["session"]["token_count"] == 6
    assert replay["session"]["model_call_count"] == 2


@pytest.mark.asyncio
async def test_credential_secret_is_private_assignment_bound_and_revocable(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    secret = "credential-value-that-must-never-enter-events"
    metadata = await storage.provision_credential(
        "platform-token",
        secret,
        credential_ref="credential.assignment.1",
        assignment_id="assignment-1",
    )
    assert "value" not in metadata
    assert all("value" not in item for item in await storage.list_credentials())
    with pytest.raises(LiliesAccessDeniedError):
        await storage.get_credential(metadata["credential_ref"], assignment_id="assignment-2")
    private = await storage.get_credential(
        metadata["credential_ref"], assignment_id="assignment-1"
    )
    assert private["value"] == secret
    assert secret not in json.dumps(await storage.list_security_events())

    revoked = await storage.revoke_credential(metadata["credential_ref"])
    assert revoked["revoked_at"] is not None
    with pytest.raises(LiliesAuthenticationError, match="revoked"):
        await storage.get_credential(metadata["credential_ref"], assignment_id="assignment-1")
    assert storage.db_path.stat().st_mode & 0o777 == 0o600
    assert storage.db_path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_v1_credentials_migrate_to_latest_without_losing_private_value(tmp_path) -> None:
    data_dir = tmp_path / "legacy"
    legacy = LiliesStorage(data_dir)
    with legacy._connect() as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            )
            """
        )
        legacy._migrate_v1(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO credentials(
              ref,name,secret_value,assignment_id,created_at,updated_at
            ) VALUES (?,?,?,?,?,?)
            """,
            ("legacy.credential", "platform_assignment", "legacy-private", "assignment-v1", now, now),
        )

    upgraded = LiliesStorage(data_dir)
    assert await upgraded.initialize() == {
        "schema_version": 3,
        "interrupted_sessions": 0,
        "interrupted_turns": 0,
    }
    with sqlite3.connect(upgraded.db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(credentials)").fetchall()
        }
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert {
        "kind",
        "scopes_json",
        "provision_idempotency_key",
        "provision_payload_hash",
        "revoke_idempotency_key",
        "revoke_payload_hash",
        "revoke_reason",
    } <= columns
    assert versions == [1, 2, 3]
    assert user_version == 3
    metadata = (await upgraded.list_credentials())[0]
    assert metadata["kind"] == "platform_assignment"
    assert metadata["scopes"] == []
    private = await upgraded.get_credential(
        "legacy.credential", assignment_id="assignment-v1"
    )
    assert private["value"] == "legacy-private"


@pytest.mark.asyncio
async def test_credential_kind_scopes_and_mutation_idempotency_are_durable(tmp_path) -> None:
    storage = LiliesStorage(tmp_path / "data")
    await storage.initialize()
    secret = "credential-secret-in-hash-only"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    provision_args = {
        "credential_ref": "credential.idempotent.1",
        "assignment_id": "assignment-1",
        "expires_at": expires_at,
        "scopes": ["workflow.run:execute", "workflow.application:write"],
        "idempotency_key": "credential-provision-0001",
    }
    created = await storage.provision_credential(
        "platform_assignment",
        secret,
        **provision_args,
    )
    assert created["kind"] == "platform_assignment"
    assert created["scopes"] == [
        "workflow.application:write",
        "workflow.run:execute",
    ]
    assert created["replayed"] is False
    assert secret not in json.dumps(created)

    replay = await storage.provision_credential(
        "platform_assignment",
        secret,
        **provision_args,
    )
    assert replay["credential_ref"] == created["credential_ref"]
    assert replay["replayed"] is True
    with pytest.raises(LiliesConflictError, match="provision idempotency"):
        await storage.provision_credential(
            "platform_assignment",
            secret + "-changed",
            **provision_args,
        )

    restarted = LiliesStorage(storage.data_dir)
    await restarted.initialize()
    persisted = (await restarted.list_credentials())[0]
    assert persisted["kind"] == "platform_assignment"
    assert persisted["scopes"] == created["scopes"]
    assert "value" not in persisted
    assert secret not in json.dumps(await restarted.list_security_events())

    revoked = await restarted.revoke_credential(
        created["credential_ref"],
        idempotency_key="credential-revoke-0001",
        reason="assignment_cancelled",
    )
    assert revoked["replayed"] is False
    assert revoked["revoke_reason"] == "assignment_cancelled"
    revoke_replay = await restarted.revoke_credential(
        created["credential_ref"],
        idempotency_key="credential-revoke-0001",
        reason="assignment_cancelled",
    )
    assert revoke_replay["replayed"] is True
    with pytest.raises(LiliesConflictError, match="revoke idempotency"):
        await restarted.revoke_credential(
            created["credential_ref"],
            idempotency_key="credential-revoke-0001",
            reason="different_reason",
        )
