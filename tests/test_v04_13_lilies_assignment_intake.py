from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from agent_platform.lilies_api import create_lilies_app
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_daemon_client import LiliesDaemonClient
from agent_platform.lilies_models import (
    AssignmentSubmissionResult,
    BuildAssignment,
    SessionCancelRequest,
    SessionCreateRequest,
    SessionKind,
)
from agent_platform.lilies_service import LocalLiliesService
from agent_platform.lilies_storage import (
    LiliesAccessDeniedError,
    LiliesConflictError,
    LiliesStorage,
)
from agent_platform.models import StreamEvent
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


DIGEST = "sha256:" + "a" * 64
PLATFORM_SCOPES = [
    "lilies.session:read",
    "lilies.session:write",
    "lilies.credential:write",
]
WORKFLOW_SCOPES = ["workflow.catalog:read"]


class BlockingProvider(ModelProvider):
    name = "blocking"

    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, True, False, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 1}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": "done"}},
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        )


class ImmediateProvider(ModelProvider):
    name = "immediate"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, True, False, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 1}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": "done"}},
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        )


def assignment_payload(
    *,
    assignment_id: str | None = None,
    idempotency_key: str | None = None,
    credential_ref: str = "credential.assignment.intake",
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc)
    deadline_at = created_at + timedelta(hours=1)
    return {
        "schema_version": "1.0",
        "assignment_id": assignment_id or str(uuid4()),
        "idempotency_key": idempotency_key or f"assignment:{uuid4().hex}",
        "mode": "customer",
        "requirement": "Build a customer-visible enterprise reconciliation workflow.",
        "business_context": {
            "customer_roles": ["operations manager"],
            "business_goal": "Reconcile approved records and produce a reviewable result.",
            "inputs": ["approved records"],
            "outputs": ["reconciliation report"],
            "constraints": ["ambiguous records require human review"],
        },
        "target": {"mode": "create_new"},
        "platform": {
            "base_url": "http://127.0.0.1:8001",
            "contract_url": "/api/v1/lilies/platform-contract",
            "contract_digest": DIGEST,
            "credential_ref": credential_ref,
            "scopes": WORKFLOW_SCOPES,
            "application_ids": [],
        },
        "constraints": {
            "deadline_at": deadline_at.isoformat(),
            "max_turns": 5,
            "max_budget_usd": 1.0,
            "max_tool_calls": 20,
            "network_policy": "allowlist",
            "allowed_hosts": ["127.0.0.1"],
            "allowed_actions": ["platform_contract_get"],
            "prohibited_actions": [
                "read_platform_source",
                "read_hidden_oracle",
                "write_task_package",
            ],
            "no_substitute_validation": False,
        },
        "deliverables": [
            {
                "name": "reconciliation report",
                "description": "A customer-visible structured reconciliation report.",
                "media_type": "application/json",
                "required": True,
            }
        ],
        "created_at": created_at.isoformat(),
    }


async def paired_client(
    storage: LiliesStorage,
    settings: LiliesSettings,
    *,
    name: str = "platform",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    requested = scopes or PLATFORM_SCOPES
    code = await storage.create_pairing_code(allowed_scopes=requested)
    return await storage.exchange_pairing_code(
        code["pairing_code"],
        name,
        requested,
        f"nonce-{uuid4().hex}",
        settings.daemon_fingerprint(),
    )


async def platform_session(
    service: LocalLiliesService,
    client_id: str,
) -> str:
    result = await service.create_session(
        SessionCreateRequest(
            idempotency_key=f"session:{uuid4().hex}",
            kind=SessionKind.platform,
        ),
        client_id=client_id,
    )
    return str(result["id"])


async def provision_for_assignment(
    service: LocalLiliesService,
    client_id: str,
    assignment: BuildAssignment,
    *,
    scopes: list[str] | None = None,
) -> None:
    await service.storage.provision_credential(
        "platform_assignment",
        "private-platform-task-token-value",
        scopes=scopes or WORKFLOW_SCOPES,
        idempotency_key=f"credential:{uuid4().hex}",
        credential_ref=assignment.platform.credential_ref,
        client_id=client_id,
        assignment_id=str(assignment.assignment_id),
        expires_at=assignment.constraints.deadline_at + timedelta(minutes=5),
    )


async def wait_for_status(
    storage: LiliesStorage,
    session_id: str,
    status: str,
) -> dict[str, Any]:
    async with asyncio.timeout(3):
        while True:
            session = await storage.get_session(session_id)
            if session["status"] == status:
                return session
            await asyncio.sleep(0.01)


def test_assignment_wire_contract_and_receipt_are_strict_and_customer_safe() -> None:
    assignment = BuildAssignment.model_validate(assignment_payload())
    projection = assignment.model_dump(mode="json", exclude_none=True)
    assert "collaboration" not in projection
    assert "task_package" not in projection
    assert "fixture_refs" not in projection

    nested_extra = copy.deepcopy(projection)
    nested_extra["platform"]["access_token"] = "plaintext-platform-token"
    with pytest.raises(ValidationError, match="forbidden sensitive field"):
        BuildAssignment.model_validate(nested_extra)

    explicit_collaboration = copy.deepcopy(projection)
    explicit_collaboration["collaboration"] = None
    with pytest.raises(ValidationError, match="must completely omit collaboration"):
        BuildAssignment.model_validate(explicit_collaboration)

    result = AssignmentSubmissionResult(
        assignment_id=assignment.assignment_id,
        session_id=uuid4(),
        turn_id=uuid4(),
        start_message_id=uuid4(),
        status="running",
        event_cursor=4,
        accepted_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AssignmentSubmissionResult.model_validate(
            {**result.model_dump(mode="json"), "internal_path": "/private/db"}
        )


@pytest.mark.parametrize(
    "plaintext",
    [
        f"lpt_{'a' * 32}_{'B' * 43}",
        f"44a1c188-d1ff-4ec2-a92e-2ad29be1a001.{'C' * 43}",
        "Authorization: Bearer this-must-never-cross-the-assignment-wire",
        "TRPQCHRU4WH6-GNM5F38HUZRHRUG8BU2RLSJY",
        "oracle://protected/hidden-seed-answer.json",
    ],
)
def test_assignment_rejects_plaintext_secret_or_protected_oracle_values(
    plaintext: str,
) -> None:
    payload = assignment_payload()
    payload["requirement"] = (
        "Build the enterprise reconciliation workflow without exposing " + plaintext
    )

    with pytest.raises(ValidationError, match="forbidden plaintext"):
        BuildAssignment.model_validate(payload)


@pytest.mark.asyncio
async def test_assignment_acceptance_links_session_message_turn_and_events_without_collaboration(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "lilies", model="test")
    provider = ImmediateProvider()
    service = LocalLiliesService(settings, provider=provider)
    await service.initialize()
    client = await paired_client(service.storage, settings)
    session_id = await platform_session(service, str(client["client_id"]))
    assignment = BuildAssignment.model_validate(assignment_payload())
    await provision_for_assignment(service, str(client["client_id"]), assignment)

    receipt = await service.submit_assignment(
        session_id,
        assignment,
        client_id=str(client["client_id"]),
    )
    assert receipt["replayed"] is False
    assert receipt["status"] == "running"
    assert receipt["session_id"] == session_id
    assert receipt["assignment_id"] == str(assignment.assignment_id)
    UUID(receipt["turn_id"])
    UUID(receipt["start_message_id"])
    await wait_for_status(service.storage, session_id, "ready")

    session = await service.storage.get_session(session_id)
    turn = await service.storage.get_turn(receipt["turn_id"])
    messages = await service.storage.list_messages(session_id)
    events = await service.storage.list_events(session_id)
    stored = await service.storage.get_assignment(str(assignment.assignment_id))
    assert session["assignment_id"] == str(assignment.assignment_id)
    assert turn["input_message_id"] == receipt["start_message_id"]
    assert turn["request_id"] == f"assignment:{assignment.assignment_id}"
    assert stored["start_turn_id"] == receipt["turn_id"]
    assert stored["accepted_event_cursor"] == receipt["event_cursor"]
    assignment_event = next(
        item for item in events if item["event_type"] == "assignment.accepted"
    )
    assert assignment_event["data"]["start_turn_id"] == receipt["turn_id"]
    serialized = json.dumps(
        {"messages": messages, "events": events, "assignment": stored["assignment"]},
        ensure_ascii=False,
    )
    assert "collaboration" not in serialized
    assert "private-platform-task-token-value" not in serialized


@pytest.mark.asyncio
async def test_assignment_replay_conflict_and_restart_never_start_a_second_turn(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "lilies", model="test")
    provider = BlockingProvider()
    service = LocalLiliesService(settings, provider=provider)
    await service.initialize()
    client = await paired_client(service.storage, settings)
    client_id = str(client["client_id"])
    session_id = await platform_session(service, client_id)
    assignment = BuildAssignment.model_validate(assignment_payload())
    await provision_for_assignment(service, client_id, assignment)

    first = await service.submit_assignment(session_id, assignment, client_id=client_id)
    await provider.entered.wait()
    replay = await service.submit_assignment(session_id, assignment, client_id=client_id)
    assert replay == {**first, "replayed": True}
    assert provider.calls == 1
    assert len(await service.storage.list_turns(session_id)) == 1

    changed = assignment.model_dump(mode="json", exclude_none=True)
    changed["requirement"] += " Changed payload."
    with pytest.raises(LiliesConflictError, match="different payload"):
        await service.submit_assignment(
            session_id,
            BuildAssignment.model_validate(changed),
            client_id=client_id,
        )

    await service.shutdown(reason="test_restart")
    restarted_provider = ImmediateProvider()
    restarted = LocalLiliesService(settings, provider=restarted_provider)
    assert (await restarted.initialize())["schema_version"] == 5
    after_restart = await restarted.submit_assignment(
        session_id,
        assignment,
        client_id=client_id,
    )
    assert after_restart["replayed"] is True
    assert after_restart["status"] == "interrupted"
    assert after_restart["turn_id"] == first["turn_id"]
    assert restarted_provider.calls == 0
    assert len(await restarted.storage.list_turns(session_id)) == 1


@pytest.mark.asyncio
async def test_assignment_requires_platform_session_private_owner_and_exact_scopes(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "lilies", model="test")
    service = LocalLiliesService(settings, provider=ImmediateProvider())
    await service.initialize()
    owner = await paired_client(service.storage, settings)
    owner_id = str(owner["client_id"])
    assignment = BuildAssignment.model_validate(assignment_payload())
    await provision_for_assignment(
        service,
        owner_id,
        assignment,
        scopes=["workflow.catalog:read", "workflow.application:write"],
    )

    interactive = await service.create_session(
        SessionCreateRequest(idempotency_key=f"session:{uuid4().hex}"),
        client_id=owner_id,
    )
    with pytest.raises(LiliesConflictError, match="platform session"):
        await service.submit_assignment(
            str(interactive["id"]),
            assignment,
            client_id=owner_id,
        )

    session_id = await platform_session(service, owner_id)
    with pytest.raises(LiliesAccessDeniedError, match="exactly match"):
        await service.submit_assignment(session_id, assignment, client_id=owner_id)
    assert (await service.storage.get_session(session_id))["assignment_id"] is None
    assert await service.storage.list_turns(session_id) == []

    stranger = await paired_client(
        service.storage,
        settings,
        name="platform:other",
    )
    stranger_id = str(stranger["client_id"])
    stranger_session = await platform_session(service, stranger_id)
    with pytest.raises(LiliesAccessDeniedError, match="another local client"):
        await service.submit_assignment(
            stranger_session,
            assignment,
            client_id=stranger_id,
        )


@pytest.mark.asyncio
async def test_assignment_cancel_is_durable_and_replay_stays_cancelled(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "lilies", model="test")
    provider = BlockingProvider()
    service = LocalLiliesService(settings, provider=provider)
    await service.initialize()
    owner = await paired_client(service.storage, settings)
    owner_id = str(owner["client_id"])
    session_id = await platform_session(service, owner_id)
    assignment = BuildAssignment.model_validate(assignment_payload())
    await provision_for_assignment(service, owner_id, assignment)
    first = await service.submit_assignment(session_id, assignment, client_id=owner_id)
    await provider.entered.wait()

    cancelled = await service.cancel_session(
        session_id,
        SessionCancelRequest(
            idempotency_key=f"cancel:{uuid4().hex}",
            reason="platform assignment cancelled",
        ),
        client_id=owner_id,
    )
    assert cancelled["status"] == "cancelled"
    stored = await service.storage.get_assignment(str(assignment.assignment_id))
    assert stored["status"] == "cancelled"
    assert stored["cancel_reason"] == "platform assignment cancelled"
    replay = await service.submit_assignment(session_id, assignment, client_id=owner_id)
    assert replay["replayed"] is True
    assert replay["status"] == "cancelled"
    assert replay["turn_id"] == first["turn_id"]
    assert provider.calls == 1
    events = await service.storage.list_events(session_id)
    assert sum(item["event_type"] == "assignment.cancelled" for item in events) == 1


@pytest.mark.asyncio
async def test_v3_database_migrates_assignment_receipts_and_survives_restart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "legacy-v3"
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
        legacy._migrate_v2(conn)
        legacy._migrate_v3(conn)

    upgraded = LiliesStorage(data_dir)
    assert (await upgraded.initialize())["schema_version"] == 5
    with sqlite3.connect(upgraded.db_path) as conn:
        versions = [
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert versions == [1, 2, 3, 4, 5]
    assert user_version == 5
    assert "assignments" in tables


@pytest.mark.asyncio
async def test_assignment_endpoint_scope_and_async_client_return_strict_receipt(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "api", model="test")
    provider = BlockingProvider()
    app = create_lilies_app(settings, provider=provider)
    service = app.state.lilies_service
    await service.initialize()
    owner = await paired_client(service.storage, settings)
    owner_id = str(owner["client_id"])
    session_id = await platform_session(service, owner_id)
    assignment = BuildAssignment.model_validate(assignment_payload())
    await provision_for_assignment(service, owner_id, assignment)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 41000))
    client = LiliesDaemonClient(
        base_url="http://127.0.0.1:8765",
        access_token=str(owner["access_token"]),
        transport=transport,
    )
    receipt = await client.submit_assignment(session_id, assignment)
    assert isinstance(receipt, AssignmentSubmissionResult)
    assert receipt.assignment_id == assignment.assignment_id
    assert receipt.session_id == UUID(session_id)
    assert receipt.status.value == "running"
    assert "access_token" not in repr(client)

    reader = await paired_client(
        service.storage,
        settings,
        name="read-only",
        scopes=["lilies.session:read"],
    )
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8765",
        transport=transport,
    ) as http:
        forbidden = await http.post(
            f"/local/v1/sessions/{session_id}/assignments",
            json=assignment.model_dump(mode="json", exclude_none=True),
            headers={"authorization": f"Bearer {reader['access_token']}"},
        )
    assert forbidden.status_code == 403
    await provider.entered.wait()
    await service.shutdown(reason="test_cleanup")
