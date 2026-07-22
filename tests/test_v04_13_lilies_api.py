from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from agent_platform.lilies_api import (
    _durable_sse_stream,
    _event_cursor,
    create_lilies_app,
)
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import (
    CredentialProvisionResult,
    CredentialRevokeResult,
    DaemonHealth,
    DaemonStatus,
    DaemonStopResult,
    PairingCodeResult,
    PairingExchangeResult,
    PermissionDecisionResult,
    SessionAckResult,
    SessionListResult,
    SessionOperationResult,
    SessionResult,
)


READ = "lilies.session:read"
WRITE = "lilies.session:write"
CONTROL = "lilies.daemon:control"
CREDENTIAL = "lilies.credential:write"
PERMISSION = "lilies.permission:resolve"


def _pair(client: TestClient, *scopes: str, name: str = "test-client") -> dict[str, Any]:
    code_response = client.post(
        "/local/v1/pairings/code",
        json={"allowed_scopes": list(scopes), "ttl_seconds": 600},
    )
    assert code_response.status_code == 200, code_response.text
    code = code_response.json()
    PairingCodeResult.model_validate(code)
    response = client.post(
        "/local/v1/pairings/exchange",
        json={
            "pairing_code": code["pairing_code"],
            "client_name": name,
            "requested_scopes": list(scopes),
            "client_nonce": secrets.token_urlsafe(24),
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    PairingExchangeResult.model_validate(result)
    return result


def _auth(pairing: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {pairing['access_token']}"}


@pytest.fixture
def local_client(tmp_path: Path) -> TestClient:
    settings = LiliesSettings(
        data_dir=tmp_path / "lilies",
        workspace_root=tmp_path / "workspaces",
        event_poll_seconds=0.01,
    )
    with TestClient(create_lilies_app(settings)) as client:
        yield client


def test_health_is_loopback_public_minimal_and_strict_requests(local_client: TestClient) -> None:
    response = local_client.get("/local/v1/health")

    assert response.status_code == 200
    DaemonHealth.model_validate(response.json())
    assert response.json() == {
        "schema_version": "1.0",
        "service": "lilies",
        "status": "ok",
        "daemon_version": "0.4.13",
    }
    serialized = response.text.casefold()
    assert all(
        forbidden not in serialized
        for forbidden in ("session_id", "access_token", "credential", "secret")
    )

    invalid = local_client.post(
        "/local/v1/pairings/code",
        json={
            "allowed_scopes": [READ],
            "ttl_seconds": 600,
            "unknown": "smuggled",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"][0]["type"] == "extra_forbidden"

    ordinary = _pair(local_client, WRITE, name="ordinary")
    hidden = local_client.post(
        "/local/v1/sessions",
        headers=_auth(ordinary),
        json={
            "idempotency_key": "ordinary-session-000001",
            "collaboration": {"channel": "must-not-be-discoverable"},
        },
    )
    assert hidden.status_code == 422
    assert hidden.json()["detail"][0]["msg"] == "request validation failed"
    assert "collaboration" not in hidden.text.casefold()
    assert local_client.get("/openapi.json").status_code == 404


@pytest.mark.asyncio
async def test_every_route_rejects_a_non_loopback_peer(tmp_path: Path) -> None:
    app = create_lilies_app(LiliesSettings(data_dir=tmp_path / "lilies"))
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 43120))
    async with httpx.AsyncClient(transport=transport, base_url="http://daemon") as client:
        response = await client.get("/local/v1/health")

    assert response.status_code == 403
    assert response.json() == {"detail": "loopback access required"}


def test_pairing_is_one_time_scope_bound_and_auth_is_uniform(local_client: TestClient) -> None:
    code = local_client.post(
        "/local/v1/pairings/code",
        json={"allowed_scopes": [READ], "ttl_seconds": 600},
    ).json()
    PairingCodeResult.model_validate(code)
    exchange_payload = {
        "pairing_code": code["pairing_code"],
        "client_name": "scope-test",
        "requested_scopes": [READ],
        "client_nonce": secrets.token_urlsafe(24),
    }
    exchanged = local_client.post("/local/v1/pairings/exchange", json=exchange_payload)
    assert exchanged.status_code == 200
    PairingExchangeResult.model_validate(exchanged.json())
    assert exchanged.json()["granted_scopes"] == [READ]
    assert exchanged.json()["daemon_fingerprint"] == code["daemon_fingerprint"]

    replay_payload = {**exchange_payload, "client_nonce": secrets.token_urlsafe(24)}
    replay = local_client.post("/local/v1/pairings/exchange", json=replay_payload)
    assert replay.status_code == 401
    assert replay.json() == {"detail": "invalid local client credentials"}

    missing = local_client.get("/local/v1/status")
    assert missing.status_code == 401
    assert missing.json() == {"detail": "invalid local client credentials"}

    write_only = _pair(local_client, WRITE)
    forbidden = local_client.get("/local/v1/status", headers=_auth(write_only))
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "forbidden"}
    assert "collaboration" not in forbidden.text.casefold()


def test_pairing_api_replays_prepared_initial_and_rotation_receipts_after_crash(
    local_client: TestClient,
) -> None:
    initial_code = local_client.post(
        "/local/v1/pairings/code",
        json={"allowed_scopes": [READ, WRITE], "ttl_seconds": 600},
    ).json()
    initial_client_id = uuid4()
    initial_token = f"{initial_client_id}." + "initial" * 8
    initial_payload = {
        "pairing_code": initial_code["pairing_code"],
        "client_name": "platform",
        "requested_scopes": [READ, WRITE],
        "client_nonce": secrets.token_urlsafe(24),
        "requested_client_id": str(initial_client_id),
        "prepared_access_token": initial_token,
    }
    initial = local_client.post(
        "/local/v1/pairings/exchange", json=initial_payload
    )
    initial_replay = local_client.post(
        "/local/v1/pairings/exchange", json=initial_payload
    )
    assert initial.status_code == 200, initial.text
    assert initial_replay.status_code == 200, initial_replay.text
    assert initial_replay.json() == initial.json()
    assert initial.json()["access_token"] == initial_token

    owner = _pair(local_client, READ, WRITE, name="platform")
    rotation_code = local_client.post(
        "/local/v1/pairings/code",
        json={"allowed_scopes": [READ, WRITE], "ttl_seconds": 600},
    ).json()
    rotated_token = f"{owner['client_id']}." + "rotated" * 8
    rotation_payload = {
        "pairing_code": rotation_code["pairing_code"],
        "client_name": "platform",
        "requested_scopes": [READ, WRITE],
        "client_nonce": secrets.token_urlsafe(24),
        "previous_client_id": owner["client_id"],
        "previous_access_token": owner["access_token"],
        "requested_client_id": owner["client_id"],
        "prepared_access_token": rotated_token,
    }
    rotated = local_client.post(
        "/local/v1/pairings/exchange", json=rotation_payload
    )
    assert rotated.status_code == 200, rotated.text

    # A platform crash may leave only its already-replaced bearer.  Replaying
    # the stable operation with that bearer as previous proof is still exact.
    rotation_replay_payload = {
        **rotation_payload,
        "previous_access_token": rotated_token,
    }
    rotated_replay = local_client.post(
        "/local/v1/pairings/exchange", json=rotation_replay_payload
    )
    assert rotated_replay.status_code == 200, rotated_replay.text
    assert rotated_replay.json() == rotated.json()

    mismatch = local_client.post(
        "/local/v1/pairings/exchange",
        json={
            **initial_payload,
            "prepared_access_token": f"{initial_client_id}." + "different" * 8,
        },
    )
    assert mismatch.status_code == 401
    assert initial_token not in mismatch.text
    assert rotated_token not in mismatch.text
    database_bytes = local_client.app.state.lilies_storage.db_path.read_bytes()
    assert initial_token.encode() not in database_bytes
    assert rotated_token.encode() not in database_bytes


def test_replacing_daemon_identity_key_rejects_old_bearer_and_records_security_event(
    local_client: TestClient,
) -> None:
    paired = _pair(local_client, READ, name="identity-bound-client")
    settings = local_client.app.state.settings
    old_fingerprint = settings.daemon_fingerprint()
    settings.identity_key_file.write_bytes(secrets.token_bytes(32))
    assert settings.daemon_fingerprint() != old_fingerprint

    rejected = local_client.get("/local/v1/status", headers=_auth(paired))
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "invalid local client credentials"}

    events = local_client.portal.call(
        local_client.app.state.lilies_storage.list_security_events
    )
    assert any(
        event["event_type"] == "client.authentication_rejected"
        and event["client_id"] == paired["client_id"]
        and event["details"] == {"reason": "daemon_fingerprint_mismatch"}
        for event in events
    )


def test_session_routes_apply_client_acl_and_project_internal_state(
    local_client: TestClient,
) -> None:
    owner = _pair(local_client, READ, WRITE, name="owner")
    stranger = _pair(local_client, READ, name="stranger")
    created = local_client.post(
        "/local/v1/sessions",
        headers=_auth(owner),
        json={
            "idempotency_key": "create-session-00000001",
            "kind": "interactive",
            "title": "Operations workflow",
        },
    )
    assert created.status_code == 201, created.text
    session = created.json()
    SessionResult.model_validate(session)
    assert session["status"] == "ready"
    assert session["kind"] == "interactive"
    assert session["title"] == "Operations workflow"
    assert "config" not in session
    assert "assignment" not in session

    owner_read = local_client.get(
        f"/local/v1/sessions/{session['session_id']}", headers=_auth(owner)
    )
    assert owner_read.status_code == 200
    SessionResult.model_validate(owner_read.json())
    assert owner_read.json() == session

    owner_listing = local_client.get("/local/v1/sessions", headers=_auth(owner))
    assert owner_listing.status_code == 200
    SessionListResult.model_validate(owner_listing.json())
    assert owner_listing.json() == {"sessions": [session]}

    denied = local_client.get(
        f"/local/v1/sessions/{session['session_id']}", headers=_auth(stranger)
    )
    assert denied.status_code == 403
    assert denied.json() == {"detail": "forbidden"}

    listing = local_client.get("/local/v1/sessions", headers=_auth(stranger))
    assert listing.status_code == 200
    SessionListResult.model_validate(listing.json())
    assert listing.json() == {"sessions": []}

    acknowledged = local_client.post(
        f"/local/v1/sessions/{session['session_id']}/acks",
        headers=_auth(owner),
        json={"idempotency_key": "session-ack-00000000001", "cursor": 1},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    ack = SessionAckResult.model_validate(acknowledged.json())
    assert str(ack.client_id) == owner["client_id"]
    assert ack.cursor == 1


def test_expired_token_repair_preserves_acl_only_with_old_client_proof(
    local_client: TestClient,
) -> None:
    owner = _pair(local_client, READ, WRITE, name="cli:api-rotation")
    created = local_client.post(
        "/local/v1/sessions",
        headers=_auth(owner),
        json={"idempotency_key": "rotation-session-000001", "title": "Persistent"},
    )
    assert created.status_code == 201
    session_id = created.json()["session_id"]
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db_path = local_client.app.state.lilies_storage.db_path
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE clients SET expires_at=? WHERE id=?",
            (expired, owner["client_id"]),
        )
    assert local_client.get("/local/v1/sessions", headers=_auth(owner)).status_code == 401

    code = local_client.post(
        "/local/v1/pairings/code",
        json={"allowed_scopes": [READ, WRITE], "ttl_seconds": 600},
    ).json()
    rotation = local_client.post(
        "/local/v1/pairings/exchange",
        json={
            "pairing_code": code["pairing_code"],
            "client_name": "cli:api-rotation",
            "requested_scopes": [READ, WRITE],
            "client_nonce": secrets.token_urlsafe(24),
            "previous_client_id": owner["client_id"],
            "previous_access_token": owner["access_token"],
        },
    )
    assert rotation.status_code == 200, rotation.text
    rotated = rotation.json()
    assert rotated["client_id"] == owner["client_id"]
    listing = local_client.get("/local/v1/sessions", headers=_auth(rotated))
    assert listing.status_code == 200
    assert [item["session_id"] for item in listing.json()["sessions"]] == [session_id]
    attached = local_client.get(
        f"/local/v1/sessions/{session_id}", headers=_auth(rotated)
    )
    assert attached.status_code == 200

    rejected_code = local_client.post(
        "/local/v1/pairings/code",
        json={"allowed_scopes": [READ], "ttl_seconds": 600},
    ).json()
    wrong_token = f"{owner['client_id']}." + "wrong" * 10
    rejected = local_client.post(
        "/local/v1/pairings/exchange",
        json={
            "pairing_code": rejected_code["pairing_code"],
            "client_name": "cli:api-rotation",
            "requested_scopes": [READ],
            "client_nonce": secrets.token_urlsafe(24),
            "previous_client_id": owner["client_id"],
            "previous_access_token": wrong_token,
        },
    )
    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "invalid local client credentials"}
    assert wrong_token not in rejected.text

    unproven = local_client.post(
        "/local/v1/pairings/exchange",
        json={
            "pairing_code": rejected_code["pairing_code"],
            "client_name": "cli:api-rotation",
            "requested_scopes": [READ],
            "client_nonce": secrets.token_urlsafe(24),
        },
    ).json()
    assert unproven["client_id"] != owner["client_id"]
    assert local_client.get("/local/v1/sessions", headers=_auth(unproven)).json() == {
        "sessions": []
    }


def test_status_stop_and_credential_responses_never_project_secrets(
    local_client: TestClient,
) -> None:
    client_record = _pair(local_client, READ, CONTROL, CREDENTIAL, name="platform")
    headers = _auth(client_record)

    status_response = local_client.get("/local/v1/status", headers=headers)
    assert status_response.status_code == 200
    daemon_status = status_response.json()
    DaemonStatus.model_validate(daemon_status)
    assert daemon_status["provider"] == "deepseek"
    assert daemon_status["paired_client_count"] >= 1
    assert daemon_status["stopping"] is False

    rejected_secret = "validation-secret-never-echo"
    malformed = local_client.post(
        "/local/v1/credentials/provision",
        headers=headers,
        json={
            "idempotency_key": "credential-invalid-00001",
            "credential_ref": "credential:invalid-test",
            "assignment_id": str(uuid4()),
            "kind": "platform_assignment",
            "secret": rejected_secret,
            "scopes": ["workflow.catalog:read"],
            "expires_at": "not-a-datetime",
        },
    )
    assert malformed.status_code == 422
    assert rejected_secret not in malformed.text

    secret_value = "top-secret-assignment-token"
    assignment_id = uuid4()
    provisioned = local_client.post(
        "/local/v1/credentials/provision",
        headers=headers,
        json={
            "idempotency_key": "credential-create-000001",
            "credential_ref": "credential:assignment-test",
            "assignment_id": str(assignment_id),
            "kind": "platform_assignment",
            "secret": secret_value,
            "scopes": ["workflow.catalog:read", "workflow.draft:write"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
    )
    assert provisioned.status_code == 201, provisioned.text
    projection = provisioned.json()
    CredentialProvisionResult.model_validate(projection)
    assert projection["credential_ref"] == "credential:assignment-test"
    assert projection["assignment_id"] == str(assignment_id)
    assert projection["kind"] == "platform_assignment"
    assert projection["scopes"] == ["workflow.catalog:read", "workflow.draft:write"]
    assert "secret" not in json.dumps(projection).casefold()
    assert secret_value not in provisioned.text

    revoked = local_client.post(
        "/local/v1/credentials/revoke",
        headers=headers,
        json={
            "idempotency_key": "credential-revoke-000001",
            "credential_ref": "credential:assignment-test",
            "reason": "assignment completed",
        },
    )
    assert revoked.status_code == 200
    CredentialRevokeResult.model_validate(revoked.json())
    assert revoked.json()["revoked"] is True

    stop_calls: list[bool] = []
    local_client.app.state.request_daemon_stop = lambda: stop_calls.append(True)
    stopped = local_client.post(
        "/local/v1/control/stop",
        headers=headers,
        json={
            "idempotency_key": "daemon-stop-000000001",
            "reason": "test shutdown",
            "cancel_active_turns": True,
            "grace_period_seconds": 0,
        },
    )
    assert stopped.status_code == 202, stopped.text
    DaemonStopResult.model_validate(stopped.json())
    assert stopped.json()["accepted"] is True
    assert stop_calls == [True]


def test_session_operation_responses_conform_to_the_strict_wire_contract(
    local_client: TestClient,
) -> None:
    client_record = _pair(local_client, READ, WRITE, name="operation-client")
    headers = _auth(client_record)
    service = local_client.app.state.lilies_service
    service._start_turn_task = lambda *args, **kwargs: None

    message_session = local_client.post(
        "/local/v1/sessions",
        headers=headers,
        json={"idempotency_key": "operation-session-000001"},
    ).json()
    sent = local_client.post(
        f"/local/v1/sessions/{message_session['session_id']}/messages",
        headers=headers,
        json={
            "idempotency_key": "operation-message-000001",
            "message_id": str(uuid4()),
            "content": "Start the bounded operation.",
        },
    )
    assert sent.status_code == 202, sent.text
    assert SessionOperationResult.model_validate(sent.json()).status.value == "running"

    cancelled = local_client.post(
        f"/local/v1/sessions/{message_session['session_id']}/cancel",
        headers=headers,
        json={
            "idempotency_key": "operation-cancel-0000001",
            "reason": "wire contract check",
        },
    )
    assert cancelled.status_code == 202, cancelled.text
    assert SessionOperationResult.model_validate(cancelled.json()).status.value == "cancelled"

    resume_session = local_client.post(
        "/local/v1/sessions",
        headers=headers,
        json={"idempotency_key": "resume-session-000000001"},
    ).json()
    storage = local_client.app.state.lilies_storage
    local_client.portal.call(
        storage.transition_session,
        resume_session["session_id"],
        "error",
    )
    resumed = local_client.post(
        f"/local/v1/sessions/{resume_session['session_id']}/resume",
        headers=headers,
        json={
            "idempotency_key": "operation-resume-0000001",
            "expected_status": "error",
            "reason": "wire contract check",
        },
    )
    assert resumed.status_code == 202, resumed.text
    assert SessionOperationResult.model_validate(resumed.json()).status.value == "running"


def test_permission_decision_actual_response_conforms_to_the_strict_wire_contract(
    local_client: TestClient,
) -> None:
    client_record = _pair(
        local_client,
        READ,
        WRITE,
        PERMISSION,
        name="permission-client",
    )
    headers = _auth(client_record)
    created = local_client.post(
        "/local/v1/sessions",
        headers=headers,
        json={"idempotency_key": "permission-session-00001"},
    ).json()
    session_id = created["session_id"]
    storage = local_client.app.state.lilies_storage

    async def prepare_permission() -> dict[str, Any]:
        turn = await storage.create_turn(
            session_id,
            "permission-request-000001",
            "permission-turn-000000001",
        )
        tool_input = {"record": 7}
        payload = json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        input_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        return await storage.create_permission_request(
            session_id,
            turn["id"],
            "connector.write",
            input_digest,
            tool_call_id="permission-call-1",
            tool_input=tool_input,
            input_summary=tool_input,
        )

    pending = local_client.portal.call(prepare_permission)
    local_client.app.state.lilies_service._start_turn_task = lambda *args, **kwargs: None
    decision = local_client.post(
        f"/local/v1/sessions/{session_id}/permissions/{pending['id']}",
        headers=headers,
        json={
            "idempotency_key": "permission-decision-0001",
            "behavior": "deny",
            "expected_input_digest": pending["input_digest"],
            "message": "Denied for contract verification.",
        },
    )
    assert decision.status_code == 200, decision.text
    result = PermissionDecisionResult.model_validate(decision.json())
    assert result.status.value == "denied"
    assert str(result.request_id) == pending["id"]


class _EventStorage:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def list_events(self, session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"session_id": session_id, **kwargs})
        return [
            {
                "session_id": session_id,
                "seq": 11,
                "event_type": "turn.completed",
                "data": {"result": "first-unacked"},
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "session_id": session_id,
                "seq": 12,
                "event_type": "turn.completed",
                "data": {"result": "second-unacked"},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ]


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.asyncio
async def test_sse_replays_database_events_and_cursor_uses_ack_and_last_event_id() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"last-event-id", b"12")],
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 5000),
        "scheme": "http",
        "http_version": "1.1",
    }
    request = Request(scope)
    cursor = _event_cursor(request, query_after=40, persisted_ack=10)
    assert cursor == 10

    storage = _EventStorage()
    stream = _durable_sse_stream(
        storage=storage,  # type: ignore[arg-type]
        session_id="session-1",
        client_id="client-1",
        after=cursor,
        request=_ConnectedRequest(),  # type: ignore[arg-type]
        poll_seconds=0.01,
    )
    first = await anext(stream)
    second = await anext(stream)
    await stream.aclose()
    assert first == (
        'id: 11\nevent: turn.completed\ndata: {"result":"first-unacked"}\n\n'
    )
    assert second == (
        'id: 12\nevent: turn.completed\ndata: {"result":"second-unacked"}\n\n'
    )
    assert storage.calls == [
        {
            "session_id": "session-1",
            "after": 10,
            "limit": 1000,
            "client_id": "client-1",
        }
    ]
