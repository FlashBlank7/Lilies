from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_platform.collaboration_models import (
    SenderRole,
    VerificationClaim,
    VerificationClaimRequest,
)
from agent_platform.collaboration_service import (
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.collaboration_storage import (
    CollaborationConflict,
    CollaborationStore,
)
from agent_platform.lilies_models import CollaborationScope
from tests.test_v04_13_collaboration_sqlite_integration import (
    DIGEST_A,
    _control_message,
    _store_with_channel,
    _verification_claim_message,
)


@pytest.mark.asyncio
async def test_raw_created_channel_cannot_bypass_atomic_activation(
    tmp_path: Path,
) -> None:
    store = CollaborationStore(tmp_path / "raw-created.db")
    await store.initialize()
    channel_id = uuid4()
    created_at = datetime.now(timezone.utc)
    base = {
        "channel_id": str(channel_id),
        "task_id": "EXP-RAW-CREATED-001",
        "task_revision": 1,
        "assignment_id": str(uuid4()),
        "lilies_session_id": str(uuid4()),
        "application_ids": [str(uuid4())],
        "approval_mode": "manual",
        "status": "created",
        "revision": 1,
        "next_seq": 1,
        "created_at": created_at.isoformat(),
        "retention_until": (created_at + timedelta(days=1)).isoformat(),
    }
    created = await store.create_channel(base)
    assert created["status"] == "created"

    with pytest.raises(CollaborationConflict):
        await store.append_message(_control_message(channel_id, "raw-write-denied"))

    active = {**base, "channel_id": str(uuid4()), "status": "active"}
    with pytest.raises(CollaborationConflict, match="activate_channel"):
        await store.create_channel(active)


@pytest.mark.asyncio
async def test_legacy_unbound_channel_is_migrated_to_read_only_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-unbound.db"
    channel_id = uuid4()
    assignment_id = uuid4()
    session_id = uuid4()
    created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    retention_until = created_at + timedelta(days=30)
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE collaboration_schema(
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            INSERT INTO collaboration_schema VALUES(1,'2026-07-01T00:00:00+00:00');
            CREATE TABLE collaboration_channels(
              channel_id TEXT PRIMARY KEY,
              task_id TEXT NOT NULL,
              task_revision INTEGER NOT NULL,
              assignment_id TEXT NOT NULL UNIQUE,
              lilies_session_id TEXT NOT NULL UNIQUE,
              approval_mode TEXT NOT NULL,
              status TEXT NOT NULL,
              revision INTEGER NOT NULL,
              next_seq INTEGER NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              closed_at TEXT,
              retention_until TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO collaboration_channels(
              channel_id,task_id,task_revision,assignment_id,lilies_session_id,
              approval_mode,status,revision,next_seq,metadata_json,created_at,
              updated_at,retention_until
            ) VALUES(?,?,?,?,?,'manual','active',1,1,'{}',?,?,?)
            """,
            (
                str(channel_id),
                "EXP-LEGACY-UNBOUND-001",
                1,
                str(assignment_id),
                str(session_id),
                created_at.isoformat(),
                created_at.isoformat(),
                retention_until.isoformat(),
            ),
        )

    store = CollaborationStore(database)
    await store.initialize()
    migrated = await store.get_channel(channel_id)
    assert migrated["application_ids"] == []
    assert (migrated["status"], migrated["revision"]) == ("closed", 2)
    assert migrated["closed_at"] is not None
    exported = await store.export_channel(channel_id)
    assert exported["channel"] == migrated
    assert exported["audit"][0]["details"] == {
        "reason": "legacy_channel_missing_application_binding"
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT application_ids_json,metadata_json FROM collaboration_channels "
            "WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (
            "[]",
            '{"closure_reason":"legacy_channel_missing_application_binding"}',
        )

    with pytest.raises(CollaborationConflict):
        await store.append_message(_control_message(channel_id, "legacy-write-denied"))
    claim_message = _verification_claim_message(
        channel_id,
        assignment_id,
        "legacy-claim-denied",
    )
    claim_record = {
        **claim_message["payload"],
        "expected_channel_revision": migrated["revision"],
        "idempotency_key": claim_message["idempotency_key"],
        "payload": claim_message["payload"],
    }
    with pytest.raises(CollaborationConflict):
        await store.create_verification_claim(claim_record, claim_message)


@pytest.mark.asyncio
async def test_storage_boundary_redacts_plaintext_before_database_write(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    leaked = _control_message(channel_id, "plaintext-secret-denied")
    leaked["payload"]["reason"] = "Authorization: Bearer plaintext-storage-secret"

    persisted_message = await store.append_message(leaked)
    assert persisted_message["payload"]["reason"] == "[REDACTED]"

    assert b"plaintext-storage-secret" not in database.read_bytes()
    persisted = await store.list_messages(channel_id, after_seq=0, limit=100)
    assert len(persisted) == 2


@pytest.mark.asyncio
async def test_storage_boundary_still_rejects_protected_oracle_references(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    leaked = _control_message(channel_id, "protected-oracle-denied")
    leaked["payload"]["reason"] = "read oracle://hidden/expected-answer"

    with pytest.raises(ValueError, match="protected oracle"):
        await store.append_message(leaked)

    assert b"oracle://hidden/expected-answer" not in database.read_bytes()


@pytest.mark.asyncio
async def test_first_durable_cursor_uses_the_authenticated_reader_role(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    service = CollaborationService(store=store, enabled=True)
    channel_id = UUID(channel["channel_id"])
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-first-reader",
        scopes=frozenset(),
    )
    assert (
        await service.resolve_event_cursor(
            principal=user,
            channel_id=channel_id,
            requested_after=99,
            durable=True,
        )
        == 0
    )
    await store.ack_reader(
        channel_id,
        user.sender_id,
        1,
        idempotency_key="cursor-history-replay-ack-0001",
        reader_role=user.role,
        expected_cursor_revision=0,
    )
    assert (
        await service.resolve_event_cursor(
            principal=user,
            channel_id=channel_id,
            requested_after=0,
            durable=True,
        )
        == 1
    )
    assert (
        await service.resolve_event_cursor(
            principal=user,
            channel_id=channel_id,
            requested_after=0,
            durable=False,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_draft_revision_change_invalidates_same_hash_claim_once(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    raw_claim = _verification_claim_message(
        channel_id,
        assignment_id,
        "same-hash-revision-change",
    )["payload"]
    claim = VerificationClaim.model_validate(raw_claim)
    service = CollaborationService(store=store, enabled=True)
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=channel["lilies_session_id"],
        scopes=frozenset(scope.value for scope in CollaborationScope),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    persisted = await service.submit_verification_claim(
        principal=lilies,
        channel_id=channel_id,
        request=VerificationClaimRequest(
            idempotency_key="same-hash-claim-freeze-0001",
            expected_channel_revision=channel["revision"],
            claim=claim.model_dump(
                mode="json",
                exclude={
                    "channel_id",
                    "assignment_id",
                    "claim_revision",
                    "status",
                    "created_at",
                    "invalidated_at",
                    "invalidation_reason",
                },
            ),
        ),
    )
    assert persisted["status"] == "frozen"

    invalidated = await service.invalidate_claims_for_draft(
        application_id=claim.application_id,
        assignment_id=assignment_id,
        current_draft_revision=claim.draft_revision + 1,
        current_content_hash=DIGEST_A,
        reason="draft revision changed while canonical content hash remained stable",
    )
    assert len(invalidated) == 1
    assert invalidated[0]["status"] == "invalidated"

    repeated = await service.invalidate_claims_for_draft(
        application_id=claim.application_id,
        assignment_id=assignment_id,
        current_draft_revision=claim.draft_revision + 1,
        current_content_hash=DIGEST_A,
        reason="draft revision changed while canonical content hash remained stable",
    )
    assert repeated == []
    messages = await store.list_messages(channel_id, after_seq=0, limit=100)
    assert [message["message_type"] for message in messages] == [
        "control",
        "verification_claim",
        "control",
    ]
