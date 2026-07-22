from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_platform.platform_blackbox_auth import (
    BlackboxAuditEventType,
    BlackboxAuthorizationRequest,
    BlackboxRequestState,
    PlatformBlackboxApplicationDenied,
    PlatformBlackboxAuthStore,
    PlatformBlackboxCredentialExpired,
    PlatformBlackboxCredentialRevoked,
    PlatformBlackboxIdempotencyConflict,
    PlatformBlackboxOperation,
    PlatformBlackboxRequestConflict,
    PlatformBlackboxScope,
    PlatformBlackboxScopeDenied,
    TaskCredentialGrant,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def grant(
    clock: MutableClock,
    *,
    assignment_id=None,
    session_id=None,
    application_ids=None,
    scopes=None,
) -> TaskCredentialGrant:
    return TaskCredentialGrant(
        assignment_id=assignment_id or uuid4(),
        session_id=session_id or uuid4(),
        application_ids=application_ids or [],
        scopes=scopes
        or [
            PlatformBlackboxScope.application_write,
            PlatformBlackboxScope.draft_write,
        ],
        expires_at=clock.value + timedelta(hours=1),
    )


def request_for(
    credential_grant: TaskCredentialGrant,
    application_id,
    *,
    operation: PlatformBlackboxOperation = PlatformBlackboxOperation.draft_apply,
    idempotency_key: str = "blackbox-request-0001",
    payload=None,
) -> BlackboxAuthorizationRequest:
    return BlackboxAuthorizationRequest(
        request_id=uuid4(),
        assignment_id=credential_grant.assignment_id,
        session_id=credential_grant.session_id,
        tool_call_id="tool-call-0001",
        idempotency_key=idempotency_key,
        application_id=application_id,
        operation=operation,
        contract_digest="sha256:" + "a" * 64,
        payload=payload or {"expected_revision": 3, "operation": {"kind": "add_node"}},
    )


def test_strict_grant_and_request_models_reject_smuggled_or_ambiguous_fields() -> None:
    clock = MutableClock()
    with pytest.raises(ValidationError):
        TaskCredentialGrant.model_validate(
            {
                **grant(clock).model_dump(mode="json"),
                "scopes": ["workflow.draft:write", "workflow.draft:write"],
            }
        )
    with pytest.raises(ValidationError):
        TaskCredentialGrant.model_validate(
            {**grant(clock).model_dump(mode="json"), "plaintext_token": "forbidden"}
        )
    with pytest.raises(ValidationError):
        BlackboxAuthorizationRequest.model_validate(
            {
                **request_for(grant(clock), uuid4()).model_dump(mode="json"),
                "operation": "platform_sql_execute",
            }
        )


@pytest.mark.asyncio
async def test_credential_persists_only_hash_and_constant_time_verifier_is_always_used(
    tmp_path,
) -> None:
    clock = MutableClock()
    store = PlatformBlackboxAuthStore(tmp_path / "agent_platform.db", clock=clock)
    assert await store.initialize() == {"schema_version": 1}
    application_id = uuid4()
    credential_grant = grant(clock, application_ids=[application_id])
    issued = await store.issue_credential(credential_grant)
    token = issued.access_token.get_secret_value()

    assert token not in repr(issued)
    with sqlite3.connect(store.db_path) as connection:
        row = connection.execute(
            "SELECT * FROM platform_task_credentials WHERE id=?",
            (str(issued.credential.credential_id),),
        ).fetchone()
        database_dump = "\n".join(connection.iterdump())
    assert row is not None
    assert token not in database_dump
    assert len(row[5]) == 64  # salted PBKDF2 verifier, never the bearer token

    auth_request = request_for(credential_grant, application_id)
    with patch(
        "agent_platform.platform_blackbox_auth.hmac.compare_digest",
        wraps=__import__("hmac").compare_digest,
    ) as comparison:
        await store.authorize_request(issued.access_token, auth_request)
        with pytest.raises(Exception, match="credential is invalid"):
            await store.authorize_request(
                "malformed", request_for(credential_grant, application_id)
            )
    assert comparison.call_count >= 2


@pytest.mark.asyncio
async def test_scope_application_expiry_and_revoke_fail_closed_and_are_audited(tmp_path) -> None:
    clock = MutableClock()
    store = PlatformBlackboxAuthStore(tmp_path / "agent_platform.db", clock=clock)
    await store.initialize()
    application_id = uuid4()
    other_application_id = uuid4()
    credential_grant = grant(
        clock,
        application_ids=[application_id],
        scopes=[PlatformBlackboxScope.draft_write],
    )
    issued = await store.issue_credential(credential_grant)

    with pytest.raises(PlatformBlackboxScopeDenied):
        await store.authorize_request(
            issued.access_token,
            request_for(
                credential_grant,
                application_id,
                operation=PlatformBlackboxOperation.publish,
                idempotency_key="blackbox-scope-deny-1",
            ),
        )
    with pytest.raises(PlatformBlackboxApplicationDenied):
        await store.authorize_request(
            issued.access_token,
            request_for(
                credential_grant,
                other_application_id,
                idempotency_key="blackbox-app-denial-1",
            ),
        )

    clock.value += timedelta(hours=2)
    with pytest.raises(PlatformBlackboxCredentialExpired):
        await store.authorize_request(
            issued.access_token,
            request_for(
                credential_grant,
                application_id,
                idempotency_key="blackbox-expired-0001",
            ),
        )
    clock.value -= timedelta(hours=2)
    revoked = await store.revoke_credential(
        issued.credential.credential_ref,
        reason="assignment cancelled",
    )
    assert revoked.revoked_at == clock.value
    with pytest.raises(PlatformBlackboxCredentialRevoked):
        await store.authorize_request(
            issued.access_token,
            request_for(
                credential_grant,
                application_id,
                idempotency_key="blackbox-revoked-0001",
            ),
        )

    reasons = [event.reason_code for event in await store.list_audit()]
    assert reasons == [
        "scope_denied",
        "application_denied",
        "credential_expired",
        "credential_revoked",
    ]


@pytest.mark.asyncio
async def test_idempotency_replays_original_result_and_conflicting_payload_is_rejected(
    tmp_path,
) -> None:
    clock = MutableClock()
    store = PlatformBlackboxAuthStore(tmp_path / "agent_platform.db", clock=clock)
    await store.initialize()
    application_id = uuid4()
    credential_grant = grant(clock, application_ids=[application_id])
    issued = await store.issue_credential(credential_grant)
    original_request = request_for(credential_grant, application_id)

    reserved = await store.authorize_request(issued.access_token, original_request)
    assert reserved.replayed is False
    assert reserved.state.value == "reserved"
    completed = await store.complete_request(
        reserved.authorization_id,
        status_code=200,
        result={"revision": 4, "content_hash": "sha256:" + "b" * 64},
    )
    assert completed.result == {"revision": 4, "content_hash": "sha256:" + "b" * 64}

    retry = original_request.model_copy(
        update={
            "request_id": uuid4(),
            "tool_call_id": "tool-call-retry-0002",
            "contract_digest": "sha256:" + "c" * 64,
            "payload": {
                "operation": {"kind": "add_node"},
                "expected_revision": 3,
            },
        }
    )
    replay = await store.authorize_request(issued.access_token, retry)
    assert replay.replayed is True
    assert replay.authorization_id == reserved.authorization_id
    assert replay.request_id == original_request.request_id
    assert replay.tool_call_id == original_request.tool_call_id
    assert replay.contract_digest == original_request.contract_digest
    assert replay.result == completed.result

    replacement = await store.issue_credential(credential_grant)
    replacement_replay = await store.authorize_request(
        replacement.access_token,
        original_request.model_copy(update={"request_id": uuid4()}),
    )
    assert replacement_replay.replayed is True
    assert replacement_replay.authorization_id == reserved.authorization_id
    assert replacement_replay.result == completed.result

    conflict = original_request.model_copy(
        update={
            "request_id": uuid4(),
            "tool_call_id": "tool-call-conflict-0003",
            "contract_digest": "sha256:" + "d" * 64,
            "payload": {"expected_revision": 99},
        }
    )
    with pytest.raises(PlatformBlackboxIdempotencyConflict):
        await store.authorize_request(issued.access_token, conflict)
    with pytest.raises(PlatformBlackboxRequestConflict):
        await store.complete_request(
            reserved.authorization_id,
            status_code=201,
            result={"revision": 5},
        )

    with sqlite3.connect(store.db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM platform_blackbox_requests").fetchone()[0] == 1
        )

    audit = await store.list_audit(assignment_id=credential_grant.assignment_id)
    original_audit = [
        item
        for item in audit
        if item.event_type in {BlackboxAuditEventType.authorized, BlackboxAuditEventType.completed}
    ]
    assert {item.request_id for item in original_audit} == {original_request.request_id}
    assert {item.tool_call_id for item in original_audit} == {original_request.tool_call_id}
    assert {item.contract_digest for item in original_audit} == {original_request.contract_digest}

    retry_audit = next(
        item
        for item in audit
        if item.event_type is BlackboxAuditEventType.replayed
        and item.tool_call_id == retry.tool_call_id
    )
    assert retry_audit.request_id == retry.request_id
    assert retry_audit.contract_digest == retry.contract_digest
    assert retry_audit.details["original_request_id"] == str(original_request.request_id)

    conflict_audit = next(
        item
        for item in audit
        if item.event_type is BlackboxAuditEventType.denied
        and item.tool_call_id == conflict.tool_call_id
    )
    assert conflict_audit.request_id == conflict.request_id
    assert conflict_audit.contract_digest == conflict.contract_digest
    assert conflict_audit.reason_code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_nonpersistent_exact_once_response_keeps_artifact_content_out_of_database(
    tmp_path,
) -> None:
    clock = MutableClock()
    db_path = tmp_path / "agent_platform.db"
    store = PlatformBlackboxAuthStore(db_path, clock=clock)
    await store.initialize()
    application_id = uuid4()
    credential_grant = grant(
        clock,
        application_ids=[application_id],
        scopes=[PlatformBlackboxScope.artifact_read],
    )
    issued = await store.issue_credential(credential_grant)
    request = request_for(
        credential_grant,
        application_id,
        operation=PlatformBlackboxOperation.artifact_read,
        idempotency_key="artifact-no-content-ledger-0001",
        payload={"run_id": "run-1", "artifact_id": str(uuid4())},
    )
    reserved = await store.authorize_request(issued.access_token, request)
    marker = "artifact bytes stay outside sqlite"
    result = {
        "ok": True,
        "data": {"content": marker, "sha256": "sha256:" + "f" * 64},
    }
    completed = await store.complete_request(
        reserved.authorization_id,
        status_code=200,
        result=result,
        persist_result=False,
    )
    assert completed.state is BlackboxRequestState.completed
    assert completed.result is None

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT response_json,response_digest FROM platform_blackbox_requests"
        ).fetchone()
        database_dump = "\n".join(connection.iterdump())
    assert row is not None
    assert row[0] is None
    assert str(row[1]).startswith("sha256:")
    assert marker not in database_dump

    restarted = PlatformBlackboxAuthStore(db_path, clock=clock)
    await restarted.initialize()
    replay = await restarted.authorize_request(
        issued.access_token,
        request.model_copy(update={"request_id": uuid4(), "tool_call_id": "artifact-retry"}),
    )
    assert replay.replayed is True
    assert replay.state is BlackboxRequestState.completed
    assert replay.result is None
    verified = await restarted.complete_request(
        replay.authorization_id,
        status_code=200,
        result=result,
        persist_result=False,
    )
    assert verified.replayed is True
    with pytest.raises(PlatformBlackboxRequestConflict):
        await restarted.complete_request(
            replay.authorization_id,
            status_code=200,
            result={"ok": True, "data": {"content": "changed"}},
            persist_result=False,
        )


@pytest.mark.asyncio
async def test_application_create_completion_extends_whitelist_and_survives_restart(
    tmp_path,
) -> None:
    clock = MutableClock()
    db_path = tmp_path / "agent_platform.db"
    store = PlatformBlackboxAuthStore(db_path, clock=clock)
    await store.initialize()
    credential_grant = grant(clock, application_ids=[])
    issued = await store.issue_credential(credential_grant)
    created_application_id = uuid4()
    create_request = request_for(
        credential_grant,
        created_application_id,
        operation=PlatformBlackboxOperation.application_create,
        idempotency_key="blackbox-create-app-1",
        payload={"name": "Invoice workflow"},
    )
    reserved = await store.authorize_request(issued.access_token, create_request)
    await store.complete_request(
        reserved.authorization_id,
        status_code=201,
        result={"application_id": str(created_application_id)},
        created_application_id=created_application_id,
    )

    restarted = PlatformBlackboxAuthStore(db_path, clock=clock)
    await restarted.initialize()
    persisted = await restarted.get_credential(issued.credential.credential_ref)
    assert persisted.application_ids == [created_application_id]
    create_replay = await restarted.authorize_request(
        issued.access_token,
        create_request.model_copy(update={"request_id": uuid4()}),
    )
    assert create_replay.replayed is True
    assert create_replay.authorization_id == reserved.authorization_id
    assert create_replay.result == {"application_id": str(created_application_id)}
    with pytest.raises(PlatformBlackboxApplicationDenied):
        await restarted.authorize_request(
            issued.access_token,
            request_for(
                credential_grant,
                uuid4(),
                operation=PlatformBlackboxOperation.application_create,
                idempotency_key="blackbox-second-app-1",
                payload={"name": "Out of assignment"},
            ),
        )
    decision = await restarted.authorize_request(
        issued.access_token,
        request_for(
            credential_grant,
            created_application_id,
            idempotency_key="blackbox-after-create-1",
        ),
    )
    assert decision.replayed is False


@pytest.mark.asyncio
async def test_concurrent_application_create_atomically_reserves_one_credential_slot(
    tmp_path,
) -> None:
    clock = MutableClock()
    db_path = tmp_path / "agent_platform.db"
    first_store = PlatformBlackboxAuthStore(db_path, clock=clock)
    second_store = PlatformBlackboxAuthStore(db_path, clock=clock)
    await first_store.initialize()
    await second_store.initialize()
    credential_grant = grant(clock, application_ids=[])
    issued = await first_store.issue_credential(credential_grant)
    first_application_id = uuid4()
    second_application_id = uuid4()
    first_request = request_for(
        credential_grant,
        first_application_id,
        operation=PlatformBlackboxOperation.application_create,
        idempotency_key="blackbox-concurrent-create-1",
        payload={"name": "First concurrent application"},
    )
    second_request = request_for(
        credential_grant,
        second_application_id,
        operation=PlatformBlackboxOperation.application_create,
        idempotency_key="blackbox-concurrent-create-2",
        payload={"name": "Second concurrent application"},
    )

    results = await asyncio.gather(
        first_store.authorize_request(issued.access_token, first_request),
        second_store.authorize_request(issued.access_token, second_request),
        return_exceptions=True,
    )

    authorized = [result for result in results if not isinstance(result, BaseException)]
    denied = [result for result in results if isinstance(result, BaseException)]
    assert len(authorized) == 1
    assert len(denied) == 1
    assert isinstance(denied[0], PlatformBlackboxApplicationDenied)

    winning_application_id = (
        first_application_id
        if authorized[0].request_id == first_request.request_id
        else second_application_id
    )
    await first_store.complete_request(
        authorized[0].authorization_id,
        status_code=201,
        result={"application_id": str(winning_application_id)},
        created_application_id=winning_application_id,
    )
    persisted = await second_store.get_credential(issued.credential.credential_ref)
    assert persisted.application_ids == [winning_application_id]

    audit = await second_store.list_audit(assignment_id=credential_grant.assignment_id)
    assert [record.outcome for record in audit].count("authorized") == 1
    assert [record.outcome for record in audit].count("denied") == 1
    assert [record.outcome for record in audit].count("completed") == 1
    denial = next(record for record in audit if record.outcome == "denied")
    assert denial.reason_code == PlatformBlackboxApplicationDenied.code


@pytest.mark.asyncio
async def test_audit_rows_are_correlated_and_database_immutable(tmp_path) -> None:
    clock = MutableClock()
    store = PlatformBlackboxAuthStore(tmp_path / "agent_platform.db", clock=clock)
    await store.initialize()
    application_id = uuid4()
    credential_grant = grant(clock, application_ids=[application_id])
    issued = await store.issue_credential(credential_grant)
    auth_request = request_for(credential_grant, application_id)
    decision = await store.authorize_request(issued.access_token, auth_request)
    audit = (await store.list_audit(assignment_id=credential_grant.assignment_id))[0]

    assert audit.credential_id == issued.credential.credential_id
    assert audit.authorization_id == decision.authorization_id
    assert audit.assignment_id == credential_grant.assignment_id
    assert audit.session_id == credential_grant.session_id
    assert audit.tool_call_id == auth_request.tool_call_id
    assert audit.request_id == auth_request.request_id
    assert audit.idempotency_key == auth_request.idempotency_key
    assert audit.application_id == application_id
    assert audit.operation == auth_request.operation

    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="audit is immutable"):
            connection.execute(
                "UPDATE platform_blackbox_audit SET operation='tampered' WHERE seq=?",
                (audit.seq,),
            )
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="audit is immutable"):
            connection.execute("DELETE FROM platform_blackbox_audit WHERE seq=?", (audit.seq,))
