from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest

from agent_platform.lilies_api import create_lilies_app
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import LocalScope
from agent_platform.local_lilies_bridge import (
    BridgeAssignmentPhase,
    BridgeConnectionStatus,
    LEGACY_DAEMON_SCOPES,
    LocalLiliesBridge,
    LocalLiliesBridgeConflict,
    LocalLiliesBridgeDaemonRejected,
    LocalLiliesObservabilitySnapshot,
    LocalLiliesObservabilityUnavailable,
    LocalLiliesBridgeSecurityError,
    LocalLiliesBridgeStore,
    LocalLiliesBridgeUnavailable,
    LocalLiliesAssignment,
    LocalLiliesBuildConstraints,
    LocalLiliesRelayCursorGap,
    LocalLiliesUsagePage,
    OBSERVABILITY_DAEMON_SCOPES,
    PairLocalLiliesRequest,
    ReconnectLocalLiliesRequest,
    StartLocalLiliesBuildRequest,
)
from agent_platform.local_lilies_client import (
    LocalLiliesClientError,
    LocalLiliesHttpClient,
    LocalLiliesProtocolError,
    LocalLiliesRemoteError,
    LocalLiliesUnavailable,
)
from agent_platform.models import StreamEvent
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxAuthStore,
    PlatformBlackboxCredentialRevoked,
)
from agent_platform.platform_harness import PlatformHarness
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.storage import Storage
from agent_platform.workflow_models import ApplicationCreateRequest
from agent_platform.workflow_storage import WorkflowStorage


DIGEST = "sha256:" + "b" * 64
FINGERPRINT = "sha256:" + "c" * 64
DAEMON_INSTANCE_ID = "913b6ec2-fb77-44af-a566-56e5ae1a60a3"


def observability_payload(
    *,
    fingerprint: str = FINGERPRINT,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope": "daemon_global",
        "coverage_complete": True,
        "daemon_fingerprint": fingerprint,
        "daemon_instance_id": DAEMON_INSTANCE_ID,
        "captured_at": "2026-07-26T12:00:00+00:00",
        "activity_revision": 9,
        "model_egress_enabled": False,
        "usage": {
            "ledger_cursor": 4,
            "attempted_calls": 4,
            "recorded_calls": 2,
            "unknown_calls": 1,
            "input_tokens": 11,
            "output_tokens": 7,
            "total_tokens": 18,
            "cost_usd": 0.02,
        },
        "runtime": {
            "active_sessions": 1,
            "active_model_turns": 1,
            "active_provider_calls": 1,
            "active_development_model_calls": 0,
        },
        "startup": {
            "recovery_completed": True,
            "automatic_resume_policy": "explicit_request_only",
            "automatic_model_resume_count": 0,
            "explicit_resume_candidate_count": 2,
            "interrupted_sessions": 1,
            "interrupted_turns": 1,
            "interrupted_development_assignments": 0,
            "reconciliation_required_development_invocations": 0,
            "unreaped_development_processes": 0,
        },
    }


class InjectedCrash(RuntimeError):
    pass


class CrashOnce:
    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.triggered = False

    def __call__(self, stage: str, _: Mapping[str, str]) -> None:
        if stage == self.stage and not self.triggered:
            self.triggered = True
            raise InjectedCrash(stage)


class FakeDaemonClient:
    def __init__(self) -> None:
        self.client_id = UUID("44a1c188-d1ff-4ec2-a92e-2ad29be1a001")
        self.client_scopes = [
            "lilies.session:read",
            "lilies.session:write",
            "lilies.permission:resolve",
            "lilies.credential:write",
        ]
        self.client_expires_at = "2035-01-01T00:00:00+00:00"
        self.daemon_token = f"{self.client_id}.{secrets.token_urlsafe(32)}"
        self.sessions: dict[str, dict[str, Any]] = {}
        self.credentials: dict[str, dict[str, Any]] = {}
        self.provision_receipts: dict[str, dict[str, Any]] = {}
        self.revoke_receipts: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.submission_receipts: dict[str, dict[str, Any]] = {}
        self.resume_receipts: dict[str, dict[str, Any]] = {}
        self.resume_receipt: dict[str, Any] | None = None
        self.events: list[dict[str, Any]] = []
        self.acked = 0
        self.unavailable = False
        self.unavailable_sessions: set[str] = set()
        self.fail_ack_once = False
        self.ack_receipt: dict[str, Any] | None = None
        self.provision_calls = 0
        self.provision_side_effects = 0
        self.assignment_calls = 0
        self.assignment_side_effects = 0
        self.last_assignment_payload: dict[str, Any] | None = None
        self.resume_calls = 0
        self.cancel_calls = 0
        self.pairing_calls = 0
        self.rotation_scopes: list[str] | None = None
        self.rotation_client_id: UUID | None = None
        self.cancel_receipt: dict[str, Any] | None = None
        self.force_cancel_conflict = False
        self.emit_cancel_events = True
        self.cancel_event_assignment_id: str | None = None
        self.fail_revoke = False
        self.force_revoke_not_found = False
        self.revoke_commit_then_fail_once = False
        self.last_task_token = ""
        self.status_fingerprint = FINGERPRINT
        self.provider_credential_loaded = False
        self.pause_after: str | None = None
        self.pause_entered = asyncio.Event()
        self.pause_release = asyncio.Event()
        self.usage_payload: dict[str, Any] | None = None
        self.usage_error: LocalLiliesClientError | None = None
        self.observability_payload: dict[str, Any] | None = None
        self.observability_error: LocalLiliesClientError | None = None
        self.observability_calls = 0

    def _available(self) -> None:
        if self.unavailable:
            raise LocalLiliesUnavailable("fixture daemon is offline")

    async def _pause(self, stage: str) -> None:
        if self.pause_after != stage:
            return
        self.pause_entered.set()
        await self.pause_release.wait()

    async def exchange_pairing(self, _: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._available()
        self.pairing_calls += 1
        assert payload["pairing_code"] == "PAIR-CODE-001"
        rotating = "previous_client_id" in payload
        requested_client_id = payload.get("requested_client_id")
        if requested_client_id and not rotating:
            self.client_id = UUID(str(requested_client_id))
        prepared_access_token = payload.get("prepared_access_token")
        if prepared_access_token:
            self.daemon_token = str(prepared_access_token)
        result_client_id = (
            self.rotation_client_id
            if rotating and self.rotation_client_id is not None
            else self.client_id
        )
        result_scopes = (
            self.rotation_scopes
            if rotating and self.rotation_scopes is not None
            else payload["requested_scopes"]
        )
        self.client_id = result_client_id
        self.client_scopes = list(result_scopes)
        return {
            "client_id": str(result_client_id),
            "access_token": self.daemon_token,
            "granted_scopes": result_scopes,
            "expires_at": self.client_expires_at,
            "daemon_fingerprint": self.status_fingerprint,
        }

    async def status(self, _: str, access_token: str) -> dict[str, Any]:
        self._available()
        if access_token != self.daemon_token:
            raise LocalLiliesRemoteError(401, "invalid fixture daemon bearer")
        return {
            "schema_version": "1.0",
            "pid": 42,
            "address": "http://127.0.0.1:8765",
            "started_at": "2026-07-23T00:00:00+00:00",
            "daemon_fingerprint": self.status_fingerprint,
            "client_id": str(self.client_id),
            "client_scopes": self.client_scopes,
            "client_expires_at": self.client_expires_at,
            "provider": "fixture",
            "model": "fixture",
            "model_egress_enabled": False,
            "provider_credential_loaded": self.provider_credential_loaded,
            "paired_client_count": 1,
            "platform_paired": True,
            "active_session_count": len(self.sessions),
            "active_assignment_count": len(self.submission_receipts),
            "stopping": False,
        }

    async def usage(
        self,
        _: str,
        access_token: str,
        *,
        group_by: tuple[str, ...],
        page: int,
        page_size: int,
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        if self.usage_error is not None:
            raise self.usage_error
        if self.usage_payload is not None:
            return dict(self.usage_payload)
        canonical_group_by = [
            dimension
            for dimension in ("session", "stage", "model")
            if dimension in group_by
        ]
        return {
            "schema_version": "1.0",
            "group_by": canonical_group_by,
            "items": [],
            "page": page,
            "page_size": page_size,
            "returned_count": 0,
            "total_items": 0,
            "total_pages": 0,
            "truncated": False,
        }

    async def observability_snapshot(
        self,
        _: str,
        access_token: str,
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        self.observability_calls += 1
        if self.observability_error is not None:
            raise self.observability_error
        payload = self.observability_payload or observability_payload()
        return json.loads(json.dumps(payload))

    async def create_session(
        self, _: str, access_token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        session_id = uuid5(
            NAMESPACE_URL,
            f"lilies:session:{self.client_id}:{payload['idempotency_key']}",
        )
        now = datetime.now(timezone.utc).isoformat()
        session = self.sessions.setdefault(
            str(session_id),
            {
                "schema_version": "1.0",
                "session_id": str(session_id),
                "status": "ready",
                "kind": "platform",
                "title": payload.get("title"),
                "assignment_id": None,
                "created_at": now,
                "updated_at": now,
                "usage": {},
            },
        )
        await self._pause("session.created")
        return session

    async def get_session(self, _: str, access_token: str, session_id: str) -> dict[str, Any]:
        self._available()
        if session_id in self.unavailable_sessions:
            raise LocalLiliesUnavailable("fixture session link is offline")
        assert access_token == self.daemon_token
        return dict(self.sessions[session_id])

    async def provision_credential(
        self, _: str, access_token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        self.provision_calls += 1
        key = payload["idempotency_key"]
        prior = self.provision_receipts.get(key)
        if prior is not None:
            assert self.credentials[payload["credential_ref"]]["secret"] == payload["secret"]
            return dict(prior)
        self.provision_side_effects += 1
        self.last_task_token = payload["secret"]
        self.credentials[payload["credential_ref"]] = dict(payload)
        receipt = {
            "credential_ref": payload["credential_ref"],
            "assignment_id": payload["assignment_id"],
            "kind": payload["kind"],
            # The standalone daemon persists scopes as a canonical set and
            # returns them sorted; authority validation must therefore be
            # order-insensitive while remaining exact about membership.
            "scopes": sorted(set(payload["scopes"])),
            "expires_at": payload["expires_at"],
            "provisioned_at": datetime.now(timezone.utc).isoformat(),
            "revoked_at": None,
        }
        self.provision_receipts[key] = receipt
        await self._pause("credential.provisioned")
        return dict(receipt)

    async def revoke_credential(
        self, _: str, access_token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        if self.fail_revoke:
            raise LocalLiliesUnavailable("fixture credential revoke link dropped")
        key = payload["idempotency_key"]
        prior = self.revoke_receipts.get(key)
        if prior is not None:
            prior_payload, prior_receipt = prior
            if prior_payload != payload:
                raise LocalLiliesRemoteError(409, "revoke idempotency payload changed")
            return dict(prior_receipt)
        if self.force_revoke_not_found or payload["credential_ref"] not in self.credentials:
            raise LocalLiliesRemoteError(404, "credential not found")
        self.credentials.pop(payload["credential_ref"], None)
        receipt = {
            "credential_ref": payload["credential_ref"],
            "revoked": True,
            "revoked_at": datetime.now(timezone.utc).isoformat(),
        }
        self.revoke_receipts[key] = (dict(payload), receipt)
        if self.revoke_commit_then_fail_once:
            self.revoke_commit_then_fail_once = False
            raise LocalLiliesUnavailable("revoke response was lost after commit")
        return dict(receipt)

    async def submit_assignment(
        self,
        _: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        assert payload["target"] == {
            "mode": "existing",
            "application_id": payload["platform"]["application_ids"][0],
        }
        assert "platform_application_create" not in payload["constraints"]["allowed_actions"]
        assert "collaboration" not in payload
        self.assignment_calls += 1
        key = payload["idempotency_key"]
        prior = self.submission_receipts.get(key)
        if prior is not None:
            return {**prior, "replayed": True}
        self.assignment_side_effects += 1
        self.last_assignment_payload = json.loads(json.dumps(payload))
        now = datetime.now(timezone.utc).isoformat()
        receipt = {
            "schema_version": "1.0",
            "assignment_id": payload["assignment_id"],
            "session_id": session_id,
            "turn_id": str(uuid5(NAMESPACE_URL, f"turn:{payload['assignment_id']}")),
            "start_message_id": str(uuid5(NAMESPACE_URL, f"message:{payload['assignment_id']}")),
            "status": "running",
            "event_cursor": 1,
            "accepted_at": now,
            "replayed": False,
        }
        self.submission_receipts[key] = receipt
        self.sessions[session_id].update(
            {
                "status": "running",
                "assignment_id": payload["assignment_id"],
                "updated_at": now,
            }
        )
        await self._pause("assignment.submitted")
        return dict(receipt)

    async def resume_session(
        self,
        _: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        if self.resume_receipt is not None:
            return dict(self.resume_receipt)
        prior = self.resume_receipts.get(payload["idempotency_key"])
        if prior is not None:
            return dict(prior)
        assert self.sessions[session_id]["status"] == payload["expected_status"]
        self.resume_calls += 1
        self.sessions[session_id]["status"] = "running"
        self.sessions[session_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        receipt = {
            "session_id": session_id,
            "status": "running",
            "event_cursor": max(1, len(self.events)),
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }
        self.resume_receipts[payload["idempotency_key"]] = receipt
        return dict(receipt)

    async def cancel_session(
        self,
        _: str,
        access_token: str,
        session_id: str,
        __: dict[str, Any],
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        self.cancel_calls += 1
        if self.cancel_receipt is not None:
            return dict(self.cancel_receipt)
        if session_id not in self.sessions:
            raise LocalLiliesRemoteError(404, "session not found")
        if self.force_cancel_conflict:
            raise LocalLiliesRemoteError(409, "fixture nonterminal conflict")
        if self.sessions[session_id]["status"] in {"completed", "closed"}:
            raise LocalLiliesRemoteError(409, "session is already terminal")
        if self.sessions[session_id]["status"] == "cancelled":
            return {
                "session_id": session_id,
                "status": "cancelled",
                "event_cursor": max(1, len(self.events)),
                "accepted_at": datetime.now(timezone.utc).isoformat(),
            }
        self.sessions[session_id]["status"] = "cancelled"
        self.sessions[session_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        if self.emit_cancel_events:
            assignment_id = self.sessions[session_id].get("assignment_id")
            self.events.append(
                {
                    "seq": len(self.events) + 1,
                    "event": "session.status_changed",
                    "data": {
                        "from_status": "running",
                        "to_status": "cancelled",
                        "reason": "requested_by_user",
                    },
                }
            )
            if assignment_id is not None:
                self.events.append(
                    {
                        "seq": len(self.events) + 1,
                        "event": "assignment.cancelled",
                        "data": {
                            "assignment_id": (self.cancel_event_assignment_id or assignment_id),
                            "reason": "requested_by_user",
                        },
                    }
                )
        return {
            "session_id": session_id,
            "status": "cancelled",
            "event_cursor": max(1, len(self.events)),
            "accepted_at": datetime.now(timezone.utc).isoformat(),
        }

    async def fetch_events(
        self,
        _: str,
        access_token: str,
        __: str,
        *,
        after: int,
        max_events: int,
        wait_seconds: float = 0.25,
    ) -> list[dict[str, Any]]:
        del wait_seconds
        self._available()
        assert access_token == self.daemon_token
        return [dict(event) for event in self.events if event["seq"] > after][:max_events]

    async def acknowledge_events(
        self,
        _: str,
        access_token: str,
        session_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self._available()
        assert access_token == self.daemon_token
        if self.ack_receipt is not None:
            return dict(self.ack_receipt)
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise LocalLiliesUnavailable("ack link dropped")
        self.acked = max(self.acked, int(payload["cursor"]))
        return {
            "client_id": str(self.client_id),
            "session_id": session_id,
            "cursor": self.acked,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


class ImmediateProvider(ModelProvider):
    name = "immediate"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, True, False, False, False, 100_000, 10_000)

    async def stream(self, **_: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": "done"}},
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        )


async def platform_parts(
    tmp_path: Path,
) -> tuple[Storage, WorkflowStorage, PlatformHarness, PlatformBlackboxAuthStore]:
    storage = Storage(tmp_path / "platform")
    await storage.initialize()
    workflow = WorkflowStorage(storage)
    await workflow.initialize()
    harness = PlatformHarness(
        storage=storage,
        secret_envelope_key="test-only-local-lilies-envelope-key",
    )
    auth = PlatformBlackboxAuthStore(tmp_path / "platform" / "blackbox-auth.db")
    await auth.initialize()
    return storage, workflow, harness, auth


def bridge_for(
    tmp_path: Path,
    *,
    workflow: WorkflowStorage,
    harness: PlatformHarness,
    auth: PlatformBlackboxAuthStore,
    daemon: Any,
    fault_hook: Any = None,
    enabled: bool = True,
) -> LocalLiliesBridge:
    return LocalLiliesBridge(
        enabled=enabled,
        store=LocalLiliesBridgeStore(tmp_path / "platform" / "local-lilies-bridge.db"),
        workflow_storage=workflow,
        harness=harness,
        auth_store=auth,
        client=daemon,
        platform_base_url="http://127.0.0.1:8001",
        contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        fault_hook=fault_hook,
    )


async def pair(bridge: LocalLiliesBridge) -> Any:
    await bridge.initialize()
    return await bridge.pair_connection(
        PairLocalLiliesRequest(
            idempotency_key="pair-platform-000001",
            base_url="http://127.0.0.1:8765",
            pairing_code="PAIR-CODE-001",
            expected_daemon_fingerprint=FINGERPRINT,
        )
    )


async def empty_application(workflow: WorkflowStorage, marker: str) -> UUID:
    application = await workflow.create_application(
        ApplicationCreateRequest(
            name=f"Local Lilies {marker}",
            requirement=("Build an enterprise document review workflow with an auditable result."),
        )
    )
    return UUID(application["id"])


def build_request(connection_id: UUID, marker: str) -> StartLocalLiliesBuildRequest:
    return StartLocalLiliesBuildRequest(
        idempotency_key=f"build-{marker}-000001",
        connection_id=connection_id,
        requirement=(
            "Build an enterprise document review workflow with human escalation and "
            "an auditable structured result."
        ),
        business_context={
            "customer_roles": ["operations reviewer"],
            "business_goal": "Review incoming documents without losing ambiguous cases.",
            "inputs": ["incoming documents"],
            "outputs": ["review decision", "audit record"],
            "constraints": ["ambiguous cases require human review"],
        },
        deliverables=[
            {
                "name": "review workflow",
                "description": "Editable workflow and its auditable decision output.",
                "media_type": "application/json",
                "required": True,
            }
        ],
    )


def credential_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM platform_task_credentials").fetchone()[0])


def assert_plaintext_absent(paths: list[Path], plaintext: str) -> None:
    needle = plaintext.encode()
    for base in paths:
        for candidate in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
            if candidate.exists():
                assert needle not in candidate.read_bytes(), candidate


@pytest.mark.asyncio
async def test_bridge_store_migrates_v1_operations_and_rejects_future_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "platform" / "local-lilies-bridge.db"
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE local_lilies_bridge_schema (
              version INTEGER PRIMARY KEY,
              applied_at TEXT NOT NULL
            );
            INSERT INTO local_lilies_bridge_schema(version,applied_at)
            VALUES(1,'2026-07-23T00:00:00+00:00');
            CREATE TABLE local_lilies_connection_operations (
              connection_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              request_digest TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(connection_id, operation, idempotency_key)
            );
            """
        )
    store = LocalLiliesBridgeStore(db_path)

    assert await store.initialize() == {"schema_version": 9}
    assert await store.initialize() == {"schema_version": 9}
    with sqlite3.connect(db_path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(local_lilies_connection_operations)"
            ).fetchall()
        }
        versions = [
            int(row[0])
            for row in conn.execute(
                "SELECT version FROM local_lilies_bridge_schema ORDER BY version"
            ).fetchall()
        ]
        conn.execute(
            "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES(99,?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
    with sqlite3.connect(db_path) as conn:
        assignment_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(local_lilies_assignments)").fetchall()
        }
        connection_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(local_lilies_connections)"
            ).fetchall()
        }
    assert {"result_json", "completed_at", "requested_scopes_json"} <= columns
    assert "pairing_scope_profile_json" in connection_columns
    assert "terminal_events_drained_at" in assignment_columns
    assert "daemon_session_creation_started_at" in assignment_columns
    assert "formal_channel_close_receipt_json" in assignment_columns
    assert {
        "formal_archive_intent_json",
        "formal_archive_intent_digest",
        "formal_archive_result_json",
        "formal_claim_result_json",
        "formal_archive_completed_at",
        "formal_terminal_archive_result_json",
        "formal_terminal_archive_manifest_digest",
        "formal_terminal_archive_completed_at",
    } <= assignment_columns
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    with pytest.raises(RuntimeError, match="newer than supported"):
        await store.initialize()


@pytest.mark.asyncio
async def test_feature_gate_loopback_and_explicit_none_policy_fail_closed(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    disabled = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        enabled=False,
    )
    with pytest.raises(Exception, match="disabled"):
        await disabled.status()
    with pytest.raises(LocalLiliesBridgeSecurityError, match="loopback"):
        LocalLiliesBridge(
            enabled=True,
            store=LocalLiliesBridgeStore(tmp_path / "bad.db"),
            workflow_storage=workflow,
            harness=harness,
            auth_store=auth,
            client=daemon,
            platform_base_url="https://example.com",
            contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        )

    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "none")
    payload = build_request(connection.connection_id, "none").model_dump(mode="json")
    payload["constraints"] = {"network_policy": "none"}
    request = StartLocalLiliesBuildRequest.model_validate(payload)
    with pytest.raises(LocalLiliesBridgeSecurityError, match="allowlist"):
        await bridge.start_build(application_id, request)
    assert await bridge.list_assignments_for_application(application_id) == []

    secret_payload = build_request(connection.connection_id, "secret").model_dump(mode="json")
    secret_payload["requirement"] = (
        "Build the enterprise workflow using "
        f"lpt_{'a' * 32}_{'B' * 43} without exposing credentials."
    )
    with pytest.raises(ValueError, match="forbidden plaintext"):
        StartLocalLiliesBuildRequest.model_validate(secret_payload)


@pytest.mark.asyncio
async def test_customer_build_projects_explicit_connector_authority(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
    )
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "customer-connectors")
    request = build_request(connection.connection_id, "customer-connectors")
    request.constraints = LocalLiliesBuildConstraints(
        allowed_hosts=["127.0.0.1"],
        model_access=False,
        connector_access=True,
        readable_host_objects=["erp.orders.list"],
        writable_host_operations=["erp.orders.update"],
        permission_required_actions=["erp.orders.update"],
        max_write_count=12,
        max_payload_bytes=2 * 1024 * 1024,
    )

    assignment = await bridge.start_build(application_id, request)

    credential_ref = (
        "platform-task-credential:"
        f"{uuid5(NAMESPACE_URL, f'lilies:platform-task-credential:{assignment.assignment_id}')}"
    )
    credential = await auth.get_credential(credential_ref)
    assert credential.connector_access is True
    assert credential.model_access is False
    assert credential.allowed_network_hosts == ["127.0.0.1"]
    assert credential.readable_host_objects == ["erp.orders.list"]
    assert credential.writable_host_operations == ["erp.orders.update"]
    assert credential.permission_required_actions == ["erp.orders.update"]
    assert credential.max_write_count == 12
    assert credential.max_payload_bytes == 2 * 1024 * 1024
    assert credential.allowed_actions_digest is not None
    assert credential.budget_digest is not None
    assert "platform_connector_authorization_issue" in {
        operation.value for operation in credential.allowed_operations
    }
    assert daemon.last_assignment_payload is not None
    constraints = daemon.last_assignment_payload["constraints"]
    assert constraints["connector_access"] is True
    assert constraints["model_access"] is False
    assert constraints["readable_host_objects"] == ["erp.orders.list"]
    assert constraints["writable_host_operations"] == ["erp.orders.update"]
    assert constraints["max_write_count"] == 12
    assert "platform_connector_authorization_issue" in constraints["allowed_actions"]


def test_customer_connector_constraints_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires connector_access"):
        LocalLiliesBuildConstraints(
            readable_host_objects=["crm.accounts.get"],
        )
    with pytest.raises(ValueError, match="require max_write_count"):
        LocalLiliesBuildConstraints(
            connector_access=True,
            writable_host_operations=["crm.accounts.update"],
        )
    with pytest.raises(ValueError, match="must be writable host operations"):
        LocalLiliesBuildConstraints(
            connector_access=True,
            readable_host_objects=["crm.accounts.get"],
            permission_required_actions=["crm.accounts.update"],
        )


@pytest.mark.asyncio
async def test_application_allows_only_one_nonterminal_assignment_at_a_time(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "single-active")
    first_request = build_request(connection.connection_id, "single-active-first")
    second_request = build_request(connection.connection_id, "single-active-second")

    requests = [first_request, second_request]
    results = await asyncio.gather(
        *(bridge.start_build(application_id, request) for request in requests),
        return_exceptions=True,
    )
    assignments = [item for item in results if isinstance(item, LocalLiliesAssignment)]
    conflicts = [item for item in results if isinstance(item, LocalLiliesBridgeConflict)]
    assert len(assignments) == 1
    assert len(conflicts) == 1
    assert "nonterminal" in str(conflicts[0])
    assert conflicts[0].details["assignment_id"] == str(assignments[0].assignment_id)
    assert daemon.assignment_side_effects == 1
    assert len(await bridge.list_assignments_for_application(application_id)) == 1
    losing_request = requests[
        next(index for index, result in enumerate(results) if isinstance(result, Exception))
    ]

    await bridge.cancel_assignment(
        assignments[0].assignment_id,
        idempotency_key="cancel-single-active-000001",
    )
    successor = await bridge.start_build(application_id, losing_request)
    assert successor.assignment_id != assignments[0].assignment_id
    assert daemon.assignment_side_effects == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_stage",
    ["pairing.exchange_accepted", "pairing.connection_committed"],
)
async def test_pairing_recovers_after_daemon_acceptance_without_pairing_code_reuse(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce(crash_stage),
    )
    await bridge.initialize()
    request = PairLocalLiliesRequest(
        idempotency_key="pair-recovery-000001",
        base_url="http://127.0.0.1:8765",
        pairing_code="PAIR-CODE-001",
        expected_daemon_fingerprint=FINGERPRINT,
    )

    with pytest.raises(InjectedCrash, match=crash_stage):
        await bridge.pair_connection(request)

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    recovered = (await restarted.list_connections())[0]
    replay = await restarted.pair_connection(request)

    assert recovered.status.value == "connected"
    assert tuple(recovered.granted_scopes) == OBSERVABILITY_DAEMON_SCOPES
    assert replay == recovered
    assert daemon.pairing_calls == 1


@pytest.mark.asyncio
async def test_legacy_pairing_outbox_recovers_exact_four_scopes_without_upgrade(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce("pairing.exchange_accepted"),
    )
    await bridge.initialize()
    request = PairLocalLiliesRequest(
        idempotency_key="legacy-pair-recovery-000001",
        base_url="http://127.0.0.1:8765",
        pairing_code="PAIR-CODE-001",
        expected_daemon_fingerprint=FINGERPRINT,
    )

    with pytest.raises(InjectedCrash, match="pairing.exchange_accepted"):
        await bridge.pair_connection(request)

    legacy_values = [scope.value for scope in LEGACY_DAEMON_SCOPES]
    with sqlite3.connect(bridge.store.db_path) as conn:
        conn.execute(
            "UPDATE local_lilies_connections "
            "SET pairing_scope_profile_json=NULL"
        )
    daemon.client_scopes = legacy_values

    restarted = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
    )
    await restarted.initialize()
    recovered = (await restarted.list_connections())[0]

    assert recovered.status is BridgeConnectionStatus.connected
    assert tuple(recovered.granted_scopes) == LEGACY_DAEMON_SCOPES
    with pytest.raises(LocalLiliesObservabilityUnavailable):
        await restarted.observability_snapshot(recovered.connection_id)
    assert daemon.pairing_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted_profile", "daemon_scopes"),
    [
        pytest.param(
            None,
            OBSERVABILITY_DAEMON_SCOPES,
            id="legacy-outbox-cannot-silently-upgrade",
        ),
        pytest.param(
            OBSERVABILITY_DAEMON_SCOPES,
            LEGACY_DAEMON_SCOPES,
            id="new-outbox-cannot-silently-downgrade",
        ),
    ],
)
async def test_pairing_outbox_recovery_rejects_scope_profile_mismatch(
    tmp_path: Path,
    persisted_profile: tuple[LocalScope, ...] | None,
    daemon_scopes: tuple[LocalScope, ...],
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce("pairing.exchange_accepted"),
    )
    await bridge.initialize()
    request = PairLocalLiliesRequest(
        idempotency_key="pair-profile-mismatch-000001",
        base_url="http://127.0.0.1:8765",
        pairing_code="PAIR-CODE-001",
        expected_daemon_fingerprint=FINGERPRINT,
    )
    with pytest.raises(InjectedCrash, match="pairing.exchange_accepted"):
        await bridge.pair_connection(request)

    raw_profile = (
        None
        if persisted_profile is None
        else json.dumps([scope.value for scope in persisted_profile])
    )
    with sqlite3.connect(bridge.store.db_path) as conn:
        conn.execute(
            "UPDATE local_lilies_connections SET pairing_scope_profile_json=?",
            (raw_profile,),
        )
    daemon.client_scopes = [scope.value for scope in daemon_scopes]

    restarted = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
    )
    await restarted.initialize()
    persisted = await restarted.store.get_connection(
        uuid5(
            NAMESPACE_URL,
            "lilies:platform-connection:"
            f"http://127.0.0.1:8765:{request.idempotency_key}",
        )
    )

    assert persisted["status"] == BridgeConnectionStatus.unavailable.value
    assert persisted["last_error_code"] == "daemon_pairing_recovery_rejected"


@pytest.mark.asyncio
async def test_reconnect_replays_durable_receipt_and_rejects_scope_or_client_downgrade(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    request = ReconnectLocalLiliesRequest(
        idempotency_key="reconnect-platform-000001",
        pairing_code="PAIR-CODE-001",
    )

    first = await bridge.reconnect_connection(connection.connection_id, request)
    replay = await bridge.reconnect_connection(connection.connection_id, request)

    assert first == replay
    assert replay.status.value == "connected"
    assert daemon.pairing_calls == 2  # initial pair plus one rotation

    daemon.rotation_scopes = ["lilies.session:read"]
    with pytest.raises(LocalLiliesBridgeSecurityError, match="required scopes"):
        await bridge.reconnect_connection(
            connection.connection_id,
            ReconnectLocalLiliesRequest(
                idempotency_key="reconnect-platform-000002",
                pairing_code="PAIR-CODE-001",
            ),
        )
    downgraded = await bridge.get_connection(connection.connection_id)
    assert downgraded.status.value == "unavailable"

    daemon.rotation_scopes = None
    daemon.rotation_client_id = UUID("44a1c188-d1ff-4ec2-a92e-2ad29be1afff")
    with pytest.raises(LocalLiliesBridgeSecurityError, match="client identity"):
        await bridge.reconnect_connection(
            connection.connection_id,
            ReconnectLocalLiliesRequest(
                idempotency_key="reconnect-platform-000003",
                pairing_code="PAIR-CODE-001",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_stage",
    [
        "reconnect.prepared_token_saved",
        "reconnect.exchange_accepted",
        "reconnect.stable_token_saved",
        "reconnect.connection_committed",
    ],
)
async def test_reconnect_recovers_every_post_rotation_crash_window(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    bridge.fault_hook = CrashOnce(crash_stage)
    request = ReconnectLocalLiliesRequest(
        idempotency_key=f"reconnect-{crash_stage.replace('.', '-')}-000001",
        pairing_code="PAIR-CODE-001",
    )

    with pytest.raises(InjectedCrash, match=crash_stage):
        await bridge.reconnect_connection(connection.connection_id, request)

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    after_initialize = await restarted.get_connection(connection.connection_id)
    if crash_stage == "reconnect.prepared_token_saved":
        assert after_initialize.status.value == "reconnecting"
    else:
        assert after_initialize.status.value == "connected"
    recovered = await restarted.reconnect_connection(connection.connection_id, request)
    assert recovered.status.value == "connected"
    assert (await restarted.refresh_connection(connection.connection_id)).status.value == (
        "connected"
    )
    assert all(
        not item["name"].startswith("daemon-access-token-rotation-")
        for item in await harness.list_secrets(
            owner_id=f"local-lilies-connection:{connection.connection_id}"
        )
    )


@pytest.mark.asyncio
async def test_legacy_reconnect_outbox_recovers_exact_four_scopes_without_upgrade(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    legacy_values = [scope.value for scope in LEGACY_DAEMON_SCOPES]
    with sqlite3.connect(bridge.store.db_path) as conn:
        conn.execute(
            "UPDATE local_lilies_connections SET granted_scopes_json=?,"
            "pairing_scope_profile_json=NULL WHERE id=?",
            (json.dumps(legacy_values), str(connection.connection_id)),
        )
    daemon.client_scopes = legacy_values
    bridge.fault_hook = CrashOnce("reconnect.exchange_accepted")
    request = ReconnectLocalLiliesRequest(
        idempotency_key="legacy-reconnect-recovery-000001",
        pairing_code="PAIR-CODE-001",
    )

    with pytest.raises(InjectedCrash, match="reconnect.exchange_accepted"):
        await bridge.reconnect_connection(connection.connection_id, request)

    with sqlite3.connect(bridge.store.db_path) as conn:
        conn.execute(
            "UPDATE local_lilies_connection_operations "
            "SET requested_scopes_json=NULL WHERE connection_id=? "
            "AND operation='reconnect' AND idempotency_key=?",
            (str(connection.connection_id), request.idempotency_key),
        )
    daemon.client_scopes = legacy_values

    restarted = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
    )
    await restarted.initialize()
    recovered = await restarted.get_connection(connection.connection_id)
    replay = await restarted.reconnect_connection(connection.connection_id, request)

    assert recovered.status is BridgeConnectionStatus.connected
    assert tuple(recovered.granted_scopes) == LEGACY_DAEMON_SCOPES
    assert replay == recovered


@pytest.mark.asyncio
async def test_new_reconnect_outbox_recovery_rejects_legacy_scope_downgrade(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    bridge.fault_hook = CrashOnce("reconnect.exchange_accepted")
    request = ReconnectLocalLiliesRequest(
        idempotency_key="new-reconnect-profile-mismatch-000001",
        pairing_code="PAIR-CODE-001",
    )

    with pytest.raises(InjectedCrash, match="reconnect.exchange_accepted"):
        await bridge.reconnect_connection(connection.connection_id, request)
    daemon.client_scopes = [scope.value for scope in LEGACY_DAEMON_SCOPES]

    restarted = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
    )
    await restarted.initialize()
    persisted = await restarted.store.get_connection(connection.connection_id)

    assert persisted["status"] == BridgeConnectionStatus.unavailable.value
    assert persisted["last_error_code"] == "daemon_reconnect_recovery_rejected"


@pytest.mark.asyncio
async def test_refresh_persists_unavailable_after_daemon_fingerprint_substitution(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    daemon.status_fingerprint = "sha256:" + "d" * 64

    with pytest.raises(LocalLiliesBridgeSecurityError, match="identity") as captured:
        await bridge.refresh_connection(connection.connection_id)
    assert captured.value.details["status"] == "unavailable"
    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status.value == "unavailable"
    assert persisted.last_error == {
        "code": "daemon_fingerprint_mismatch",
        "message": "daemon identity no longer matches the paired connection",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported_expiry", "expected_status", "message"),
    [
        ("2025-01-01T00:00:00+00:00", "expired", "has expired"),
        ("2036-01-01T00:00:00+00:00", "unavailable", "changed outside pairing"),
    ],
)
async def test_refresh_fails_closed_on_expired_or_mismatched_bearer_expiry(
    tmp_path: Path,
    reported_expiry: str,
    expected_status: str,
    message: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    daemon.client_expires_at = reported_expiry

    with pytest.raises(LocalLiliesBridgeSecurityError, match=message):
        await bridge.refresh_connection(connection.connection_id)

    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status.value == expected_status


@pytest.mark.asyncio
async def test_observability_snapshot_uses_exact_new_scope_profile_and_strict_receipt(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)

    assert tuple(connection.granted_scopes) == OBSERVABILITY_DAEMON_SCOPES
    assert tuple(daemon.client_scopes) == tuple(
        scope.value for scope in OBSERVABILITY_DAEMON_SCOPES
    )

    snapshot = await bridge.observability_snapshot(connection.connection_id)

    assert snapshot.scope == "daemon_global"
    assert snapshot.coverage_complete is True
    assert snapshot.daemon_fingerprint == FINGERPRINT
    assert str(snapshot.daemon_instance_id) == DAEMON_INSTANCE_ID
    assert snapshot.usage.attempted_calls == 4
    assert snapshot.runtime.active_provider_calls == 1
    assert snapshot.startup.automatic_resume_policy == "explicit_request_only"
    assert daemon.observability_calls == 1
    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status is BridgeConnectionStatus.connected
    assert persisted.last_error is None


def test_observability_snapshot_rejects_coercion_and_impossible_counters() -> None:
    payload = observability_payload()
    snapshot = LocalLiliesObservabilitySnapshot.model_validate_json(
        json.dumps(payload),
        strict=True,
    )
    assert snapshot.usage.total_tokens == 18

    invalid_payloads: list[dict[str, Any]] = []
    for path, invalid_value in (
        (("activity_revision",), True),
        (("activity_revision",), "9"),
        (("activity_revision",), 2**63),
        (("model_egress_enabled",), "false"),
        (("scope",), " daemon_global "),
        (("daemon_fingerprint",), f" {FINGERPRINT} "),
        (("daemon_instance_id",), f" {DAEMON_INSTANCE_ID} "),
        (("daemon_instance_id",), DAEMON_INSTANCE_ID.upper()),
        (("daemon_instance_id",), DAEMON_INSTANCE_ID.replace("-", "")),
        (("daemon_instance_id",), f"{{{DAEMON_INSTANCE_ID}}}"),
        (("daemon_instance_id",), f"urn:uuid:{DAEMON_INSTANCE_ID}"),
        (("captured_at",), "2026-07-26 12:00:00+00:00"),
        (("captured_at",), " 2026-07-26T12:00:00+00:00 "),
        (("coverage_complete",), 1),
        (("coverage_complete",), 1.0),
        (("usage", "ledger_cursor"), "4"),
        (("usage", "ledger_cursor"), 2**63),
        (("usage", "input_tokens"), True),
        (("usage", "input_tokens"), 2**63),
        (("usage", "cost_usd"), float("nan")),
        (("usage", "cost_usd"), 1_000_000_000_000.01),
        (("startup", "recovery_completed"), False),
        (("startup", "recovery_completed"), "true"),
        (("startup", "automatic_model_resume_count"), 1),
        (("startup", "automatic_model_resume_count"), False),
        (("startup", "interrupted_turns"), 2**63),
    ):
        candidate = json.loads(json.dumps(payload))
        target = candidate
        for segment in path[:-1]:
            target = target[segment]
        target[path[-1]] = invalid_value
        invalid_payloads.append(candidate)

    attempted_mismatch = json.loads(json.dumps(payload))
    attempted_mismatch["usage"]["attempted_calls"] = 3
    invalid_payloads.append(attempted_mismatch)
    total_mismatch = json.loads(json.dumps(payload))
    total_mismatch["usage"]["total_tokens"] = 19
    invalid_payloads.append(total_mismatch)
    cursor_mismatch = json.loads(json.dumps(payload))
    cursor_mismatch["usage"]["ledger_cursor"] = 3
    invalid_payloads.append(cursor_mismatch)
    unrecorded_measured_usage = json.loads(json.dumps(payload))
    unrecorded_measured_usage["usage"].update(
        attempted_calls=1,
        recorded_calls=0,
        unknown_calls=0,
        input_tokens=1,
        output_tokens=2,
        total_tokens=3,
        cost_usd=9.0,
    )
    invalid_payloads.append(unrecorded_measured_usage)
    development_exceeds_provider = json.loads(json.dumps(payload))
    development_exceeds_provider["runtime"]["active_development_model_calls"] = 2
    invalid_payloads.append(development_exceeds_provider)
    provider_exceeds_turns = json.loads(json.dumps(payload))
    provider_exceeds_turns["runtime"].update(
        active_provider_calls=2,
        active_development_model_calls=0,
    )
    provider_exceeds_turns["usage"]["attempted_calls"] = 5
    invalid_payloads.append(provider_exceeds_turns)
    turns_exceed_sessions = json.loads(json.dumps(payload))
    turns_exceed_sessions["runtime"]["active_model_turns"] = 2
    invalid_payloads.append(turns_exceed_sessions)
    non_utc = json.loads(json.dumps(payload))
    non_utc["captured_at"] = "2026-07-26T21:00:00+09:00"
    invalid_payloads.append(non_utc)
    extra_field = json.loads(json.dumps(payload))
    extra_field["bootstrap_secret"] = "must-not-be-accepted"
    invalid_payloads.append(extra_field)

    for candidate in invalid_payloads:
        with pytest.raises(ValueError):
            LocalLiliesObservabilitySnapshot.model_validate_json(
                json.dumps(candidate),
                strict=True,
            )


@pytest.mark.asyncio
async def test_legacy_connection_refreshes_without_upgrade_and_observability_is_unknown(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    legacy_values = [scope.value for scope in LEGACY_DAEMON_SCOPES]
    with sqlite3.connect(bridge.store.db_path) as conn:
        conn.execute(
            "UPDATE local_lilies_connections SET granted_scopes_json=? WHERE id=?",
            (json.dumps(legacy_values), str(connection.connection_id)),
        )
    daemon.client_scopes = legacy_values

    refreshed = await bridge.refresh_connection(connection.connection_id)
    assert tuple(refreshed.granted_scopes) == LEGACY_DAEMON_SCOPES
    assert refreshed.status is BridgeConnectionStatus.connected

    with pytest.raises(LocalLiliesObservabilityUnavailable) as captured:
        await bridge.observability_snapshot(connection.connection_id)
    assert captured.value.status_code == 409
    assert captured.value.details["availability"] == "unknown"
    assert captured.value.details["reason"] == "missing_observability_scope"
    assert daemon.observability_calls == 0
    still_connected = await bridge.get_connection(connection.connection_id)
    assert still_connected.status is BridgeConnectionStatus.connected
    assert still_connected.last_error is None

    upgraded = await bridge.reconnect_connection(
        connection.connection_id,
        ReconnectLocalLiliesRequest(
            idempotency_key="reconnect-observability-upgrade-0001",
            pairing_code="PAIR-CODE-001",
        ),
    )
    assert tuple(upgraded.granted_scopes) == OBSERVABILITY_DAEMON_SCOPES
    assert (await bridge.observability_snapshot(connection.connection_id)).coverage_complete
    assert daemon.observability_calls == 1


@pytest.mark.asyncio
async def test_refresh_accepts_explicit_daemon_provider_credential_state(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    daemon.provider_credential_loaded = True
    bridge = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
    )
    connection = await pair(bridge)

    refreshed = await bridge.refresh_connection(connection.connection_id)

    assert refreshed.status is BridgeConnectionStatus.connected


@pytest.mark.asyncio
async def test_refresh_rejects_daemon_side_scope_upgrade_without_pairing(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    legacy_values = [scope.value for scope in LEGACY_DAEMON_SCOPES]
    with sqlite3.connect(bridge.store.db_path) as conn:
        conn.execute(
            "UPDATE local_lilies_connections SET granted_scopes_json=? WHERE id=?",
            (json.dumps(legacy_values), str(connection.connection_id)),
        )
    daemon.client_scopes = [scope.value for scope in OBSERVABILITY_DAEMON_SCOPES]

    with pytest.raises(LocalLiliesBridgeSecurityError, match="persisted pairing profile"):
        await bridge.refresh_connection(connection.connection_id)

    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status is BridgeConnectionStatus.unavailable
    assert persisted.last_error == {
        "code": "daemon_scope_mismatch",
        "message": "daemon bearer scopes no longer match the persisted pairing",
    }


@pytest.mark.parametrize(
    ("daemon_error", "expected_error", "expected_status", "expected_code"),
    [
        pytest.param(
            LocalLiliesUnavailable("SENSITIVE offline detail"),
            LocalLiliesBridgeUnavailable,
            503,
            "daemon_observability_unavailable",
            id="offline",
        ),
        pytest.param(
            LocalLiliesRemoteError(401, "SENSITIVE revoked bearer"),
            LocalLiliesBridgeDaemonRejected,
            502,
            "daemon_observability_authentication_rejected",
            id="authentication-rejected",
        ),
        pytest.param(
            LocalLiliesRemoteError(403, "SENSITIVE missing scope"),
            LocalLiliesBridgeDaemonRejected,
            502,
            "daemon_observability_authentication_rejected",
            id="scope-rejected",
        ),
        pytest.param(
            LocalLiliesRemoteError(500, "SENSITIVE daemon detail"),
            LocalLiliesBridgeDaemonRejected,
            502,
            "daemon_observability_rejected",
            id="remote-failure",
        ),
        pytest.param(
            LocalLiliesProtocolError("SENSITIVE malformed response"),
            LocalLiliesBridgeDaemonRejected,
            502,
            "daemon_observability_protocol_error",
            id="protocol-failure",
        ),
    ],
)
@pytest.mark.asyncio
async def test_observability_failure_mapping_is_stateful_and_sanitized(
    tmp_path: Path,
    daemon_error: LocalLiliesClientError,
    expected_error: type[Exception],
    expected_status: int,
    expected_code: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    daemon.observability_error = daemon_error

    with pytest.raises(expected_error) as captured:
        await bridge.observability_snapshot(connection.connection_id)

    assert "SENSITIVE" not in str(captured.value)
    assert captured.value.status_code == expected_status
    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status is BridgeConnectionStatus.unavailable
    assert persisted.last_error is not None
    assert persisted.last_error["code"] == expected_code
    assert "SENSITIVE" not in json.dumps(persisted.last_error)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        pytest.param("extra", "daemon_observability_receipt_invalid", id="extra"),
        pytest.param(
            "attempts", "daemon_observability_receipt_invalid", id="attempt-mismatch"
        ),
        pytest.param(
            "tokens", "daemon_observability_receipt_invalid", id="token-mismatch"
        ),
        pytest.param(
            "fingerprint", "daemon_observability_receipt_mismatch", id="fingerprint"
        ),
    ],
)
@pytest.mark.asyncio
async def test_observability_invalid_or_mismatched_receipt_fails_closed(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    payload = observability_payload()
    if mutation == "extra":
        payload["unexpected"] = "SENSITIVE daemon field"
    elif mutation == "attempts":
        payload["usage"]["attempted_calls"] = 3
    elif mutation == "tokens":
        payload["usage"]["total_tokens"] = 19
    else:
        payload["daemon_fingerprint"] = "sha256:" + "d" * 64
    daemon.observability_payload = payload

    with pytest.raises(LocalLiliesBridgeDaemonRejected) as captured:
        await bridge.observability_snapshot(connection.connection_id)

    assert captured.value.status_code == 502
    assert "SENSITIVE" not in str(captured.value)
    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status is BridgeConnectionStatus.unavailable
    assert persisted.last_error is not None
    assert persisted.last_error["code"] == expected_code
    assert "SENSITIVE" not in json.dumps(persisted.last_error)


@pytest.mark.asyncio
async def test_usage_reads_only_authenticated_public_receipt_and_validates_totals(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)

    canonical = await bridge.usage(
        connection.connection_id,
        group_by=("model", "stage"),
    )
    assert canonical.group_by == ["stage", "model"]

    daemon.usage_payload = {
        "schema_version": "1.0",
        "group_by": ["session", "stage", "model"],
        "items": [
            {
                "session_id": str(uuid4()),
                "stage": "builder",
                "model": "fixture-model",
                "recorded_calls": 2,
                "unknown_calls": 1,
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "cost_usd": 0.02,
            }
        ],
        "page": 1,
        "page_size": 100,
        "returned_count": 1,
        "total_items": 1,
        "total_pages": 1,
        "truncated": False,
    }

    result = await bridge.usage(connection.connection_id)

    assert result.items[0].total_tokens == 18
    assert result.items[0].unknown_calls == 1
    assert result.model_dump(mode="json")["items"][0]["stage"] == "builder"

    assert daemon.usage_payload is not None
    daemon.usage_payload["items"][0]["total_tokens"] = 19
    with pytest.raises(LocalLiliesBridgeDaemonRejected, match="invalid authenticated usage"):
        await bridge.usage(connection.connection_id)

    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status.value == "unavailable"
    assert persisted.last_error == {
        "code": "daemon_usage_receipt_invalid",
        "message": "local Lilies returned an invalid usage receipt",
    }


@pytest.mark.parametrize("invalid_page", [True, "1"])
@pytest.mark.asyncio
async def test_usage_rejects_dangerous_wire_type_coercion(
    tmp_path: Path,
    invalid_page: Any,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    daemon.usage_payload = {
        "schema_version": "1.0",
        "group_by": ["session", "stage", "model"],
        "items": [],
        "page": invalid_page,
        "page_size": 100,
        "returned_count": 0,
        "total_items": 0,
        "total_pages": 0,
        "truncated": False,
    }

    with pytest.raises(LocalLiliesBridgeDaemonRejected, match="invalid authenticated usage"):
        await bridge.usage(connection.connection_id)

    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status.value == "unavailable"
    assert persisted.last_error is not None
    assert persisted.last_error["code"] == "daemon_usage_receipt_invalid"


@pytest.mark.parametrize(
    ("daemon_error", "expected_error", "expected_code"),
    [
        pytest.param(
            LocalLiliesRemoteError(401, "SENSITIVE revoked bearer"),
            LocalLiliesBridgeSecurityError,
            "daemon_usage_authentication_rejected",
            id="authentication-rejected",
        ),
        pytest.param(
            LocalLiliesRemoteError(503, "SENSITIVE daemon detail"),
            LocalLiliesBridgeDaemonRejected,
            "daemon_usage_rejected",
            id="remote-failure",
        ),
        pytest.param(
            LocalLiliesProtocolError("SENSITIVE malformed response"),
            LocalLiliesBridgeDaemonRejected,
            "daemon_usage_protocol_error",
            id="protocol-failure",
        ),
        pytest.param(
            LocalLiliesUnavailable("SENSITIVE offline detail"),
            LocalLiliesBridgeUnavailable,
            "daemon_unavailable",
            id="offline",
        ),
    ],
)
@pytest.mark.asyncio
async def test_usage_failure_mapping_is_stateful_and_sanitized(
    tmp_path: Path,
    daemon_error: LocalLiliesClientError,
    expected_error: type[Exception],
    expected_code: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    daemon.usage_error = daemon_error

    with pytest.raises(expected_error) as captured:
        await bridge.usage(connection.connection_id)

    assert "SENSITIVE" not in str(captured.value)
    persisted = await bridge.get_connection(connection.connection_id)
    assert persisted.status.value == "unavailable"
    assert persisted.last_error is not None
    assert persisted.last_error["code"] == expected_code


def test_usage_page_fails_closed_on_impossible_aggregates_and_bounded_truncation() -> None:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "group_by": ["stage"],
        "items": [
            {
                "session_id": None,
                "stage": "builder",
                "model": None,
                "recorded_calls": 0,
                "unknown_calls": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            }
        ],
        "page": 1,
        "page_size": 100,
        "returned_count": 1,
        "total_items": 1,
        "total_pages": 1,
        "truncated": False,
    }

    invalid_payloads: list[dict[str, Any]] = []
    for changes in (
        {"stage": None},
        {"model": "unrequested-model"},
        {"unknown_calls": 0},
        {"input_tokens": 1, "total_tokens": 1},
    ):
        candidate = json.loads(json.dumps(payload))
        candidate["items"][0].update(changes)
        invalid_payloads.append(candidate)
    duplicate = json.loads(json.dumps(payload))
    duplicate["items"].append(dict(duplicate["items"][0]))
    duplicate.update(returned_count=2, total_items=2)
    invalid_payloads.append(duplicate)

    for candidate in invalid_payloads:
        with pytest.raises(ValueError):
            LocalLiliesUsagePage.model_validate(candidate)

    bounded = json.loads(json.dumps(payload))
    bounded["items"] = [
        {
            **payload["items"][0],
            "stage": f"stage-{index:03d}",
        }
        for index in range(100)
    ]
    bounded.update(
        page=1000,
        returned_count=100,
        total_items=100001,
        total_pages=1000,
        truncated=True,
    )
    assert LocalLiliesUsagePage.model_validate(bounded).truncated is True
    bounded["truncated"] = False
    with pytest.raises(ValueError, match="truncated"):
        LocalLiliesUsagePage.model_validate(bounded)


@pytest.mark.asyncio
async def test_cancel_requires_strict_receipts_and_recovers_daemon_cleanup(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "cancel-receipt")
    assignment = await bridge.start_build(
        application_id, build_request(connection.connection_id, "cancel-receipt")
    )
    daemon.cancel_receipt = {}

    with pytest.raises(LocalLiliesBridgeSecurityError, match="cancellation receipt"):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="assignment-cancel-receipt-000001",
        )

    pending = await bridge.get_assignment(assignment.assignment_id)
    assert pending.desired_state.value == "cancelled"
    assert pending.phase == BridgeAssignmentPhase.unavailable
    assert str(assignment.session_id) in daemon.sessions
    assert daemon.sessions[str(assignment.session_id)]["status"] == "running"

    daemon.cancel_receipt = None
    recovered = await bridge.recover_pending_assignments()
    assert recovered.scanned == 1
    assert recovered.cancelled == 1
    assert (await bridge.get_assignment(assignment.assignment_id)).phase == (
        BridgeAssignmentPhase.cancelled
    )
    assert daemon.credentials == {}


@pytest.mark.asyncio
async def test_cancel_revoke_failure_stays_recoverable_until_daemon_confirms_cleanup(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "cancel-revoke")
    assignment = await bridge.start_build(
        application_id, build_request(connection.connection_id, "cancel-revoke")
    )
    daemon.fail_revoke = True

    with pytest.raises(LocalLiliesBridgeUnavailable, match="cancellation remains pending"):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="assignment-cancel-revoke-000001",
        )

    pending = await bridge.get_assignment(assignment.assignment_id)
    assert pending.desired_state.value == "cancelled"
    assert pending.phase == BridgeAssignmentPhase.unavailable
    assert daemon.credentials
    daemon.fail_revoke = False

    recovered = await bridge.recover_pending_assignments()
    assert recovered.scanned == 1
    assert recovered.cancelled == 1
    assert daemon.credentials == {}
    assert (await bridge.get_connection(connection.connection_id)).status.value == ("connected")


@pytest.mark.asyncio
async def test_revoke_commit_response_loss_reuses_stable_payload_during_recovery(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "revoke-response-loss")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "revoke-response-loss"),
    )
    daemon.revoke_commit_then_fail_once = True

    with pytest.raises(LocalLiliesBridgeUnavailable):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="cancel-revoke-response-loss-000001",
            reason="user supplied reason",
        )

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    recovered = await restarted.recover_pending_assignments()
    assert recovered.scanned == 1
    assert recovered.cancelled == 1
    assert recovered.failed == 0
    assert (await restarted.get_assignment(assignment.assignment_id)).phase == (
        BridgeAssignmentPhase.cancelled
    )
    assert (await restarted.get_connection(connection.connection_id)).status.value == ("connected")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_stage",
    [
        "session.created",
        "credential.issue_committed",
        "credential.provisioned",
    ],
)
async def test_cancel_cleans_uncertain_session_and_credential_crash_windows(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    crashing = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce(crash_stage),
    )
    connection = await pair(crashing)
    application_id = await empty_application(workflow, f"cancel-{crash_stage}")

    with pytest.raises(InjectedCrash, match=crash_stage):
        await crashing.start_build(
            application_id,
            build_request(connection.connection_id, f"cancel-{crash_stage}"),
        )
    assignment = (await crashing.list_assignments_for_application(application_id))[0]

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    cancelled = await restarted.cancel_assignment(
        assignment.assignment_id,
        idempotency_key=f"cancel-{crash_stage.replace('.', '-')}-000001",
    )

    assert cancelled.phase == BridgeAssignmentPhase.cancelled
    assert cancelled.status == "cancelled"
    assert daemon.sessions[str(assignment.session_id)]["status"] == "cancelled"
    assert daemon.credentials == {}


@pytest.mark.asyncio
async def test_post_provision_credential_404_fails_closed_until_recovery(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "post-provision-404")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "post-provision-404"),
    )
    daemon.force_revoke_not_found = True

    with pytest.raises(Exception, match="cancellation|rejected"):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="cancel-post-provision-404-000001",
        )
    pending = await bridge.get_assignment(assignment.assignment_id)
    assert pending.desired_state.value == "cancelled"
    assert pending.phase == BridgeAssignmentPhase.unavailable
    assert daemon.credentials

    daemon.force_revoke_not_found = False
    recovered = await bridge.recover_pending_assignments()
    assert recovered.cancelled == 1
    assert daemon.credentials == {}


@pytest.mark.asyncio
async def test_cancel_replay_is_stable_and_completed_assignment_is_immutable(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)

    cancelled_app = await empty_application(workflow, "cancel-replay")
    cancelled_assignment = await bridge.start_build(
        cancelled_app,
        build_request(connection.connection_id, "cancel-replay"),
    )
    first = await bridge.cancel_assignment(
        cancelled_assignment.assignment_id,
        idempotency_key="cancel-replay-stable-000001",
    )
    replay = await bridge.cancel_assignment(
        cancelled_assignment.assignment_id,
        idempotency_key="cancel-replay-stable-000002",
    )
    assert replay.phase == first.phase == BridgeAssignmentPhase.cancelled
    assert replay.status == first.status == "cancelled"
    assert replay.desired_state.value == "cancelled"

    completed_app = await empty_application(workflow, "completed-cancel")
    completed_assignment = await bridge.start_build(
        completed_app,
        build_request(connection.connection_id, "completed-cancel"),
    )
    daemon.sessions[str(completed_assignment.session_id)]["status"] = "completed"
    await bridge.relay_events(completed_assignment.assignment_id)
    with pytest.raises(LocalLiliesBridgeConflict, match="completed"):
        await bridge.cancel_assignment(
            completed_assignment.assignment_id,
            idempotency_key="cancel-completed-000001",
        )
    preserved = await bridge.get_assignment(completed_assignment.assignment_id)
    assert preserved.phase == BridgeAssignmentPhase.completed
    assert preserved.status == "completed"
    assert preserved.desired_state.value == "active"
    assert (await workflow.get_build(str(completed_assignment.build_id)))["status"] == ("succeeded")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("daemon_status", "expected_phase", "expected_status", "expected_desired", "build_status"),
    [
        ("completed", BridgeAssignmentPhase.completed, "completed", "active", "succeeded"),
        ("closed", BridgeAssignmentPhase.cancelled, "cancelled", "cancelled", "cancelled"),
    ],
)
async def test_daemon_terminal_state_wins_cancel_409_race(
    tmp_path: Path,
    daemon_status: str,
    expected_phase: BridgeAssignmentPhase,
    expected_status: str,
    expected_desired: str,
    build_status: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, f"cancel-race-{daemon_status}")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, f"cancel-race-{daemon_status}"),
    )
    daemon.sessions[str(assignment.session_id)]["status"] = daemon_status

    if daemon_status == "completed":
        with pytest.raises(LocalLiliesBridgeConflict, match="completed"):
            await bridge.cancel_assignment(
                assignment.assignment_id,
                idempotency_key=f"cancel-terminal-{daemon_status}-000001",
            )
    else:
        result = await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key=f"cancel-terminal-{daemon_status}-000001",
        )
        assert result.phase == expected_phase

    persisted = await bridge.get_assignment(assignment.assignment_id)
    assert persisted.phase == expected_phase
    assert persisted.status == expected_status
    assert persisted.desired_state.value == expected_desired
    assert persisted.daemon_status is not None
    assert persisted.daemon_status.value == daemon_status
    assert daemon.sessions[str(assignment.session_id)]["status"] == daemon_status
    assert (await workflow.get_build(str(assignment.build_id)))["status"] == build_status

    if daemon_status == "completed":
        assert daemon.credentials
        authenticated = await auth.authenticate_credential(daemon.last_task_token)
        assert str(authenticated.assignment_id) == str(assignment.assignment_id)
        with pytest.raises(LocalLiliesBridgeConflict, match="completed"):
            await bridge.cancel_assignment(
                assignment.assignment_id,
                idempotency_key=f"cancel-terminal-{daemon_status}-000002",
            )
    else:
        assert daemon.credentials == {}
        with pytest.raises(PlatformBlackboxCredentialRevoked):
            await auth.authenticate_credential(daemon.last_task_token)
        replay = await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key=f"cancel-terminal-{daemon_status}-000002",
        )
        assert replay == persisted


@pytest.mark.asyncio
async def test_nonterminal_cancel_409_remains_fail_closed(tmp_path: Path) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "cancel-nonterminal-409")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "cancel-nonterminal-409"),
    )
    daemon.force_cancel_conflict = True

    with pytest.raises(LocalLiliesBridgeDaemonRejected, match="rejected cancellation"):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="cancel-nonterminal-conflict-000001",
        )

    pending = await bridge.get_assignment(assignment.assignment_id)
    assert pending.phase == BridgeAssignmentPhase.unavailable
    assert pending.desired_state.value == "cancelled"
    assert daemon.sessions[str(assignment.session_id)]["status"] == "running"
    assert daemon.credentials
    with pytest.raises(PlatformBlackboxCredentialRevoked):
        await auth.authenticate_credential(daemon.last_task_token)
    assert (await bridge.get_connection(connection.connection_id)).status.value == ("unavailable")

    daemon.force_cancel_conflict = False
    recovered = await bridge.recover_pending_assignments()
    assert recovered.cancelled == 1
    assert daemon.credentials == {}


@pytest.mark.asyncio
async def test_cancel_409_terminal_receipt_mismatch_remains_fail_closed(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "cancel-terminal-mismatch")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "cancel-terminal-mismatch"),
    )
    daemon_session = daemon.sessions[str(assignment.session_id)]
    daemon_session["status"] = "completed"
    daemon_session["assignment_id"] = str(uuid4())

    with pytest.raises(LocalLiliesBridgeSecurityError, match="assignment binding"):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="cancel-terminal-mismatch-000001",
        )

    pending = await bridge.get_assignment(assignment.assignment_id)
    assert pending.phase == BridgeAssignmentPhase.unavailable
    assert pending.desired_state.value == "cancelled"
    assert (await bridge.get_connection(connection.connection_id)).status.value == ("unavailable")
    with pytest.raises(PlatformBlackboxCredentialRevoked):
        await auth.authenticate_credential(daemon.last_task_token)


@pytest.mark.asyncio
async def test_cancel_accepts_unbound_terminal_session_before_assignment_acceptance(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "cancel-unbound-terminal")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "cancel-unbound-terminal"),
    )
    daemon_session = daemon.sessions[str(assignment.session_id)]
    daemon_session["status"] = "closed"
    daemon_session["assignment_id"] = None
    daemon.events.clear()
    await bridge.store.update_assignment(
        assignment.assignment_id,
        phase="submitting",
        status="submitting",
    )

    cancelled = await bridge.cancel_assignment(
        assignment.assignment_id,
        idempotency_key="cancel-unbound-terminal-000001",
    )

    assert cancelled.phase == BridgeAssignmentPhase.cancelled
    assert cancelled.status == "cancelled"
    assert cancelled.desired_state.value == "cancelled"
    assert cancelled.daemon_status is not None
    assert cancelled.daemon_status.value == "closed"
    assert daemon.credentials == {}
    with pytest.raises(PlatformBlackboxCredentialRevoked):
        await auth.authenticate_credential(daemon.last_task_token)


@pytest.mark.asyncio
async def test_relay_finishes_pending_cancel_before_projecting_daemon_state(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "cancel-relay")
    assignment = await bridge.start_build(
        application_id, build_request(connection.connection_id, "cancel-relay")
    )
    daemon.fail_revoke = True
    with pytest.raises(LocalLiliesBridgeUnavailable):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="cancel-relay-pending-000001",
        )
    assert daemon.credentials

    daemon.fail_revoke = False
    relayed = await bridge.relay_events(assignment.assignment_id)
    assert relayed.assignment.phase == BridgeAssignmentPhase.cancelled
    assert relayed.inserted == 2
    assert relayed.relay_cursor == relayed.ack_cursor == daemon.acked == 2
    assert daemon.credentials == {}
    assert (await bridge.recover_pending_assignments()).scanned == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pause_stage",
    ["session.created", "credential.provisioned", "assignment.submitted"],
)
async def test_cross_bridge_cancel_wins_each_outward_side_effect_race(
    tmp_path: Path,
    pause_stage: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    starter = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    canceller = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(starter)
    await canceller.initialize()
    application_id = await empty_application(workflow, f"race-{pause_stage}")
    daemon.pause_after = pause_stage
    start_task = asyncio.create_task(
        starter.start_build(
            application_id,
            build_request(connection.connection_id, f"race-{pause_stage}"),
        )
    )
    await asyncio.wait_for(daemon.pause_entered.wait(), timeout=3)
    assignment = (await starter.list_assignments_for_application(application_id))[0]

    cancelled = await canceller.cancel_assignment(
        assignment.assignment_id,
        idempotency_key=f"cancel-race-{pause_stage.replace('.', '-')}-000001",
    )
    daemon.pause_release.set()
    start_result = await asyncio.wait_for(start_task, timeout=3)

    assert cancelled.phase == BridgeAssignmentPhase.cancelled
    assert start_result.phase == BridgeAssignmentPhase.cancelled
    final = await starter.get_assignment(assignment.assignment_id)
    assert final.phase == BridgeAssignmentPhase.cancelled
    assert final.desired_state.value == "cancelled"
    assert daemon.sessions[str(assignment.session_id)]["status"] == "cancelled"
    assert daemon.credentials == {}


@pytest.mark.asyncio
async def test_reserved_assignment_rechecks_empty_draft_before_restart_recovery(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    crashing = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce("assignment.reserved"),
    )
    connection = await pair(crashing)
    application_id = await empty_application(workflow, "reserved-empty")
    request = build_request(connection.connection_id, "reserved-empty")

    with pytest.raises(InjectedCrash, match="assignment.reserved"):
        await crashing.start_build(application_id, request)
    draft = await workflow.get_draft(str(application_id))
    await workflow.save_draft(
        str(application_id),
        draft["snapshot"],
        expected_revision=0,
        idempotency_key="legacy-draft-touch-000001",
    )

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    recovered = await restarted.recover_pending_assignments()

    assert recovered.scanned == 1
    assert recovered.failed == 1
    assignment = (await restarted.list_assignments_for_application(application_id))[0]
    assert assignment.phase == BridgeAssignmentPhase.failed
    assert daemon.assignment_side_effects == 0
    assert daemon.sessions == {}


@pytest.mark.asyncio
async def test_relay_rejects_cursor_gap_and_synchronizes_terminal_build_status(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "relay-gap")
    assignment = await bridge.start_build(
        application_id, build_request(connection.connection_id, "relay-gap")
    )
    daemon.events = [
        {"seq": 1, "event": "assignment.accepted", "data": {}},
        {"seq": 3, "event": "turn.finished", "data": {}},
    ]

    with pytest.raises(LocalLiliesRelayCursorGap, match="expected 2, received 3"):
        await bridge.relay_events(assignment.assignment_id)
    persisted = await bridge.get_assignment(assignment.assignment_id)
    assert persisted.relay_cursor == persisted.ack_cursor == 0
    assert await bridge.list_events(assignment.assignment_id) == []
    assert daemon.acked == 0

    daemon.events[1]["seq"] = 2
    daemon.sessions[str(assignment.session_id)]["status"] = "completed"
    bridge.fault_hook = CrashOnce("relay.assignment_state_committed")
    with pytest.raises(InjectedCrash, match="relay.assignment_state_committed"):
        await bridge.relay_events(assignment.assignment_id)
    persisted = await bridge.get_assignment(assignment.assignment_id)
    assert persisted.phase == BridgeAssignmentPhase.completed
    assert persisted.relay_cursor == persisted.ack_cursor == 2
    build = await workflow.get_build(str(assignment.build_id))
    assert build["status"] == "running"

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    recovered = await restarted.recover_pending_assignments()
    assert recovered.scanned == 0
    build = await workflow.get_build(str(assignment.build_id))
    assert build["status"] == "succeeded"


@pytest.mark.asyncio
async def test_relay_ack_cursor_advances_only_after_a_strict_bound_receipt(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "ack-receipt")
    assignment = await bridge.start_build(
        application_id, build_request(connection.connection_id, "ack-receipt")
    )
    daemon.events = [{"seq": 1, "event": "assignment.accepted", "data": {}}]
    daemon.ack_receipt = {
        "client_id": str(connection.client_id),
        "session_id": str(assignment.session_id),
        "cursor": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    with pytest.raises(LocalLiliesBridgeSecurityError, match="cursor binding"):
        await bridge.relay_events(assignment.assignment_id)
    persisted = await bridge.get_assignment(assignment.assignment_id)
    assert persisted.relay_cursor == 1
    assert persisted.ack_cursor == 0

    daemon.ack_receipt = None
    replayed = await bridge.relay_events(assignment.assignment_id)
    assert replayed.relay_cursor == replayed.ack_cursor == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("escaped_field", ["session_id", "assignment_id"])
async def test_relay_rejects_a_session_receipt_outside_the_assignment_binding(
    tmp_path: Path,
    escaped_field: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, f"relay-{escaped_field}")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, f"relay-{escaped_field}"),
    )
    daemon.sessions[str(assignment.session_id)][escaped_field] = str(uuid4())

    with pytest.raises(LocalLiliesBridgeSecurityError, match="binding"):
        await bridge.relay_events(assignment.assignment_id)

    persisted = await bridge.get_assignment(assignment.assignment_id)
    assert persisted.phase == BridgeAssignmentPhase.running
    build = await workflow.get_build(str(assignment.build_id))
    assert build["status"] == "running"


@pytest.mark.asyncio
async def test_resume_rejects_an_operation_receipt_for_another_session(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "resume-receipt-binding")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "resume-receipt-binding"),
    )
    daemon.sessions[str(assignment.session_id)].update(
        {
            "status": "error",
            "updated_at": "2026-07-23T01:00:00+00:00",
        }
    )
    daemon.resume_receipt = {
        "session_id": str(uuid4()),
        "status": "running",
        "event_cursor": 1,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }

    with pytest.raises(LocalLiliesBridgeSecurityError, match="session binding"):
        await bridge.resume_assignment(assignment.assignment_id)

    persisted = await bridge.get_assignment(assignment.assignment_id)
    assert persisted.phase == BridgeAssignmentPhase.running
    build = await workflow.get_build(str(assignment.build_id))
    assert build["status"] == "running"


@pytest.mark.asyncio
async def test_each_new_error_episode_gets_a_distinct_stable_resume_receipt(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "resume-episode")
    assignment = await bridge.start_build(
        application_id, build_request(connection.connection_id, "resume-episode")
    )
    session = daemon.sessions[str(assignment.session_id)]

    session.update({"status": "error", "updated_at": "2026-07-23T01:00:00+00:00"})
    first = await bridge.resume_assignment(assignment.assignment_id)
    assert first.phase == BridgeAssignmentPhase.running
    assert daemon.resume_calls == 1

    session.update({"status": "error", "updated_at": "2026-07-23T01:01:00+00:00"})
    second = await bridge.resume_assignment(assignment.assignment_id)
    assert second.phase == BridgeAssignmentPhase.running
    assert daemon.resume_calls == 2
    assert len(daemon.resume_receipts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_stage",
    [
        "credential.outbox_saved",
        "credential.issued",
        "credential.provisioned",
    ],
)
async def test_task_credential_outbox_recovers_save_issue_and_provision_crashes(
    tmp_path: Path,
    crash_stage: str,
) -> None:
    storage, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    crashing = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce(crash_stage),
    )
    connection = await pair(crashing)
    application_id = await empty_application(workflow, crash_stage)
    request = build_request(connection.connection_id, crash_stage.replace(".", "-"))

    with pytest.raises(InjectedCrash, match=crash_stage):
        await crashing.start_build(application_id, request)

    resumed = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await resumed.initialize()
    assignment = (await resumed.list_assignments_for_application(application_id))[0]
    result = await resumed.resume_assignment(assignment.assignment_id)

    assert result.phase == BridgeAssignmentPhase.running
    assert daemon.provision_side_effects == 1
    assert daemon.assignment_side_effects == 1
    assert credential_count(auth.db_path) == 1
    assert (
        await harness.list_secrets(owner_id=f"local-lilies-assignment:{assignment.assignment_id}")
        == []
    )
    assert daemon.last_task_token.startswith("lpt_")
    assert_plaintext_absent(
        [storage.db_path, auth.db_path, resumed.store.db_path],
        daemon.last_task_token,
    )

    replay = await resumed.start_build(application_id, request)
    assert replay.assignment_id == assignment.assignment_id
    assert daemon.provision_side_effects == 1
    assert daemon.assignment_side_effects == 1


@pytest.mark.asyncio
async def test_unavailable_preserves_four_ids_then_reconnects_without_prebuild(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "offline")
    request = build_request(connection.connection_id, "offline")
    daemon.unavailable = True

    with pytest.raises(LocalLiliesBridgeUnavailable) as captured:
        await bridge.start_build(application_id, request)
    assert {
        "application_id",
        "build_id",
        "assignment_id",
        "session_id",
        "connection_id",
    } <= captured.value.details.keys()
    assignment = (await bridge.list_assignments_for_application(application_id))[0]
    assert assignment.phase == BridgeAssignmentPhase.unavailable
    draft = await workflow.get_draft(str(application_id))
    assert draft["revision"] == 0
    assert draft["snapshot"].workflow.nodes == []

    daemon.unavailable = False
    resumed = await bridge.resume_assignment(assignment.assignment_id)
    assert resumed.phase == BridgeAssignmentPhase.running
    assert resumed.application_id == application_id


@pytest.mark.asyncio
async def test_start_idempotency_replay_never_implicitly_resumes_daemon_error(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "error-replay")
    request = build_request(connection.connection_id, "error-replay")
    assignment = await bridge.start_build(application_id, request)
    daemon.sessions[str(assignment.session_id)]["status"] = "error"

    relayed = await bridge.relay_events(assignment.assignment_id)
    assert relayed.assignment.phase == BridgeAssignmentPhase.failed
    assert daemon.resume_calls == 0

    replay = await bridge.start_build(application_id, request)
    assert replay.assignment_id == assignment.assignment_id
    assert replay.phase == BridgeAssignmentPhase.failed
    assert daemon.resume_calls == 0
    assert daemon.assignment_side_effects == 1

    resumed = await bridge.resume_assignment(assignment.assignment_id)
    assert resumed.phase == BridgeAssignmentPhase.running
    assert daemon.resume_calls == 1


@pytest.mark.asyncio
async def test_relay_commits_before_ack_replays_after_restart_and_cancel_is_durable(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "relay")
    assignment = await bridge.start_build(
        application_id, build_request(connection.connection_id, "relay")
    )
    daemon.events = [
        {"seq": 1, "event": "assignment.accepted", "data": {"phase": "start"}},
        {"seq": 2, "event": "tool.completed", "data": {"tool": "catalog"}},
    ]
    daemon.fail_ack_once = True
    with pytest.raises(LocalLiliesBridgeUnavailable):
        await bridge.relay_events(assignment.assignment_id)
    persisted = await bridge.get_assignment(assignment.assignment_id)
    assert persisted.relay_cursor == 2
    assert persisted.ack_cursor == 0
    assert [event.daemon_seq for event in await bridge.list_events(assignment.assignment_id)] == [
        1,
        2,
    ]

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    replay = await restarted.relay_events(assignment.assignment_id)
    assert replay.inserted == 0
    assert replay.ack_cursor == 2
    assert daemon.acked == 2
    assert len(await restarted.list_events(assignment.assignment_id)) == 2

    daemon.sessions[str(assignment.session_id)]["status"] = "interrupted"
    resumed = await restarted.resume_assignment(assignment.assignment_id)
    assert resumed.phase == BridgeAssignmentPhase.running
    assert daemon.resume_calls == 1

    by_build = await restarted.get_assignment_by_build(assignment.build_id)
    by_session = await restarted.get_assignment_by_session(assignment.session_id)
    assert by_build.assignment_id == by_session.assignment_id == assignment.assignment_id
    daemon.unavailable = True
    with pytest.raises(LocalLiliesBridgeUnavailable):
        await restarted.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="assignment-cancel-000001",
        )
    credential = await auth.get_credential(
        f"platform-task-credential:{uuid5(NAMESPACE_URL, f'lilies:platform-task-credential:{assignment.assignment_id}')}"
    )
    assert credential.revoked_at is not None
    with pytest.raises(PlatformBlackboxCredentialRevoked):
        await auth.authenticate_credential(daemon.last_task_token)
    assert (
        await harness.list_secrets(owner_id=f"local-lilies-assignment:{assignment.assignment_id}")
        == []
    )

    daemon.unavailable = False
    cancelled = await restarted.cancel_assignment(
        assignment.assignment_id,
        idempotency_key="assignment-cancel-000001",
    )
    assert cancelled.phase == BridgeAssignmentPhase.cancelled
    assert daemon.cancel_calls == 1


@pytest.mark.asyncio
async def test_cancel_drains_and_acks_terminal_events_with_connection_bearer(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    daemon.emit_cancel_events = True
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "terminal-drain")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "terminal-drain"),
    )

    cancelled = await bridge.cancel_assignment(
        assignment.assignment_id,
        idempotency_key="terminal-drain-cancel-000001",
    )

    events = await bridge.list_events(assignment.assignment_id)
    raw = await bridge.store.get_assignment(assignment.assignment_id)
    assert cancelled.phase == BridgeAssignmentPhase.cancelled
    assert cancelled.relay_cursor == cancelled.ack_cursor == daemon.acked == 2
    assert [event.event_type for event in events] == [
        "session.status_changed",
        "assignment.cancelled",
    ]
    assert events[-1].data["assignment_id"] == str(assignment.assignment_id)
    assert raw["terminal_events_drained_at"] is not None
    assert (
        await harness.list_secrets(owner_id=f"local-lilies-assignment:{assignment.assignment_id}")
        == []
    )


@pytest.mark.asyncio
async def test_completed_assignment_drains_and_acks_entire_terminal_tail(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, "completed-terminal-drain")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, "completed-terminal-drain"),
    )
    daemon.events = [
        {
            "seq": 1,
            "event": "assignment.accepted",
            "data": {"assignment_id": str(assignment.assignment_id)},
        },
        {
            "seq": 2,
            "event": "message.created",
            "data": {"role": "assistant", "content": "final result"},
        },
        {
            "seq": 3,
            "event": "session.status_changed",
            "data": {"from_status": "running", "to_status": "completed"},
        },
    ]
    daemon.sessions[str(assignment.session_id)]["status"] = "completed"

    relayed = await bridge.relay_events(assignment.assignment_id, max_events=1)

    raw = await bridge.store.get_assignment(assignment.assignment_id)
    events = await bridge.list_events(assignment.assignment_id)
    assert relayed.assignment.phase == BridgeAssignmentPhase.completed
    assert relayed.inserted == 3
    assert relayed.relay_cursor == relayed.ack_cursor == daemon.acked == 3
    assert [event.daemon_seq for event in events] == [1, 2, 3]
    assert raw["terminal_events_drained_at"] is not None
    assert (await workflow.get_build(str(assignment.build_id)))["status"] == "succeeded"


@pytest.mark.asyncio
async def test_cancel_terminal_drain_is_bounded_and_restart_recovers_commit_before_ack(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    daemon.emit_cancel_events = True
    crashing = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce("cancel.assignment_state_committed"),
    )
    connection = await pair(crashing)
    application_id = await empty_application(workflow, "terminal-drain-restart")
    assignment = await crashing.start_build(
        application_id,
        build_request(connection.connection_id, "terminal-drain-restart"),
    )
    with pytest.raises(InjectedCrash, match="cancel.assignment_state_committed"):
        await crashing.cancel_assignment(
            assignment.assignment_id,
            idempotency_key="terminal-drain-restart-cancel-000001",
        )

    restarted = bridge_for(
        tmp_path,
        workflow=workflow,
        harness=harness,
        auth=auth,
        daemon=daemon,
        fault_hook=CrashOnce("terminal_relay.committed_before_ack"),
    )
    await restarted.initialize()
    with pytest.raises(InjectedCrash, match="terminal_relay.committed_before_ack"):
        await restarted.relay_events(assignment.assignment_id, max_events=1)
    committed = await restarted.store.get_assignment(assignment.assignment_id)
    assert committed["phase"] == "cancelled"
    assert committed["desired_state"] == "cancelled"
    assert committed["relay_cursor"] == 1
    assert committed["ack_cursor"] == 0
    assert committed["terminal_events_drained_at"] is None

    recovered = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await recovered.initialize()
    first = await recovered.relay_events(assignment.assignment_id, max_events=1)
    assert first.assignment.phase == BridgeAssignmentPhase.cancelled
    assert first.relay_cursor == first.ack_cursor == 2
    assert (await recovered.store.get_assignment(assignment.assignment_id))[
        "terminal_events_drained_at"
    ] is None
    summary = await recovered.recover_pending_assignments()
    final = await recovered.store.get_assignment(assignment.assignment_id)
    assert summary.scanned == 1
    assert summary.cancelled == 1
    assert final["relay_cursor"] == final["ack_cursor"] == daemon.acked == 2
    assert final["terminal_events_drained_at"] is not None
    assert [event.event_type for event in await recovered.list_events(assignment.assignment_id)][
        -1
    ] == "assignment.cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["event", "ack", "session"])
async def test_cancel_terminal_drain_receipt_mismatch_fails_closed(
    tmp_path: Path,
    mismatch: str,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    daemon.emit_cancel_events = mismatch in {"event", "ack"}
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    application_id = await empty_application(workflow, f"terminal-{mismatch}")
    assignment = await bridge.start_build(
        application_id,
        build_request(connection.connection_id, f"terminal-{mismatch}"),
    )
    if mismatch == "event":
        daemon.cancel_event_assignment_id = str(uuid4())
    elif mismatch == "ack":
        daemon.ack_receipt = {
            "client_id": str(uuid4()),
            "session_id": str(assignment.session_id),
            "cursor": 2,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        daemon.sessions[str(assignment.session_id)]["assignment_id"] = str(uuid4())

    with pytest.raises(LocalLiliesBridgeSecurityError):
        await bridge.cancel_assignment(
            assignment.assignment_id,
            idempotency_key=f"terminal-{mismatch}-cancel-000001",
        )

    raw = await bridge.store.get_assignment(assignment.assignment_id)
    assert raw["phase"] == "cancelled"
    assert raw["status"] == "cancelled"
    assert raw["desired_state"] == "cancelled"
    assert raw["terminal_events_drained_at"] is None
    assert raw["last_error_code"] == "terminal_event_drain_security"


@pytest.mark.asyncio
async def test_platform_restart_recovers_each_pending_assignment_without_bypassing_waits(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    daemon = FakeDaemonClient()
    bridge = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    connection = await pair(bridge)
    assignments = []
    for marker in ("restart-interrupted", "restart-waiting", "restart-offline"):
        application_id = await empty_application(workflow, marker)
        assignments.append(
            await bridge.start_build(
                application_id,
                build_request(connection.connection_id, marker),
            )
        )

    interrupted, waiting, offline = assignments
    daemon.sessions[str(interrupted.session_id)]["status"] = "interrupted"
    daemon.sessions[str(waiting.session_id)]["status"] = "waiting_permission"
    daemon.unavailable_sessions.add(str(offline.session_id))

    restarted = bridge_for(tmp_path, workflow=workflow, harness=harness, auth=auth, daemon=daemon)
    await restarted.initialize()
    summary = await restarted.recover_pending_assignments()

    assert summary.scanned == 3
    assert summary.recovered == 1
    assert summary.waiting == 1
    assert summary.unavailable == 1
    assert summary.failed == 0
    assert daemon.resume_calls == 1
    waiting_item = next(
        item for item in summary.assignments if item.assignment_id == waiting.assignment_id
    )
    assert waiting_item.phase == BridgeAssignmentPhase.waiting
    assert daemon.sessions[str(waiting.session_id)]["status"] == "waiting_permission"
    encoded = summary.model_dump_json()
    assert daemon.daemon_token not in encoded
    assert daemon.last_task_token not in encoded


@pytest.mark.asyncio
async def test_real_daemon_asgi_recovers_pair_and_reconnect_response_loss(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    settings = LiliesSettings(
        data_dir=tmp_path / "daemon",
        workspace_root=tmp_path / "daemon-workspaces",
        model="fixture-model",
        event_poll_seconds=0.01,
    )
    app = create_lilies_app(settings, provider=ImmediateProvider())
    await app.state.lilies_service.initialize()
    try:
        required_scopes = [
            "lilies.session:read",
            "lilies.session:write",
            "lilies.permission:resolve",
            "lilies.credential:write",
            "lilies.observability:read",
        ]
        client = LocalLiliesHttpClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 43121))
        )
        bridge_path = tmp_path / "platform" / "local-lilies-bridge.db"

        pairing = await app.state.lilies_storage.create_pairing_code(allowed_scopes=required_scopes)
        pair_request = PairLocalLiliesRequest(
            idempotency_key="real-pair-response-loss-0001",
            base_url="http://127.0.0.1:8765",
            pairing_code=pairing["pairing_code"],
            expected_daemon_fingerprint=settings.daemon_fingerprint(),
        )
        crashing_pair_bridge = LocalLiliesBridge(
            enabled=True,
            store=LocalLiliesBridgeStore(bridge_path),
            workflow_storage=workflow,
            harness=harness,
            auth_store=auth,
            client=client,
            platform_base_url="http://127.0.0.1:8001",
            contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
            fault_hook=CrashOnce("pairing.exchange_accepted"),
        )
        await crashing_pair_bridge.initialize()
        with pytest.raises(InjectedCrash, match="pairing.exchange_accepted"):
            await crashing_pair_bridge.pair_connection(pair_request)

        recovered_pair_bridge = LocalLiliesBridge(
            enabled=True,
            store=LocalLiliesBridgeStore(bridge_path),
            workflow_storage=workflow,
            harness=harness,
            auth_store=auth,
            client=client,
            platform_base_url="http://127.0.0.1:8001",
            contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        )
        await recovered_pair_bridge.initialize()
        connection = (await recovered_pair_bridge.list_connections())[0]
        paired_client = await app.state.lilies_storage.get_client(str(connection.client_id))

        assert connection.status.value == "connected"
        assert paired_client["client_id"] == str(connection.client_id)
        assert set(paired_client["scopes"]) == set(required_scopes)
        assert set(scope.value for scope in connection.granted_scopes) == set(required_scopes)
        assert connection.expires_at is not None
        assert connection.expires_at.isoformat() == paired_client["expires_at"]
        assert await recovered_pair_bridge.pair_connection(pair_request) == connection
        assert [
            event["event_type"] for event in await app.state.lilies_storage.list_security_events()
        ].count("pairing.exchange_replayed") == 0

        rotation = await app.state.lilies_storage.create_pairing_code(
            allowed_scopes=required_scopes
        )
        reconnect_request = ReconnectLocalLiliesRequest(
            idempotency_key="real-reconnect-response-loss-0001",
            pairing_code=rotation["pairing_code"],
        )
        recovered_pair_bridge.fault_hook = CrashOnce("reconnect.exchange_accepted")
        with pytest.raises(InjectedCrash, match="reconnect.exchange_accepted"):
            await recovered_pair_bridge.reconnect_connection(
                connection.connection_id, reconnect_request
            )

        recovered_reconnect_bridge = LocalLiliesBridge(
            enabled=True,
            store=LocalLiliesBridgeStore(bridge_path),
            workflow_storage=workflow,
            harness=harness,
            auth_store=auth,
            client=client,
            platform_base_url="http://127.0.0.1:8001",
            contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        )
        await recovered_reconnect_bridge.initialize()
        reconnected = await recovered_reconnect_bridge.get_connection(connection.connection_id)
        rotated_client = await app.state.lilies_storage.get_client(str(reconnected.client_id))
        replay = await recovered_reconnect_bridge.reconnect_connection(
            connection.connection_id, reconnect_request
        )

        assert replay == reconnected
        assert reconnected.status.value == "connected"
        assert reconnected.client_id == connection.client_id
        assert rotated_client["client_id"] == str(reconnected.client_id)
        assert set(rotated_client["scopes"]) == set(required_scopes)
        assert set(scope.value for scope in reconnected.granted_scopes) == set(required_scopes)
        assert reconnected.expires_at is not None
        assert reconnected.expires_at.isoformat() == rotated_client["expires_at"]
        owner_id = f"local-lilies-connection:{connection.connection_id}"
        assert all(
            not item["name"].startswith("daemon-access-token-rotation-")
            for item in await harness.list_secrets(owner_id=owner_id)
        )
        assert (
            await recovered_reconnect_bridge.store.list_pending_connection_operations(
                operation="reconnect"
            )
            == []
        )
        security_event_types = [
            event["event_type"] for event in await app.state.lilies_storage.list_security_events()
        ]
        assert security_event_types.count("pairing.exchanged") == 1
        assert security_event_types.count("pairing.rotated") == 1
        assert security_event_types.count("pairing.exchange_replayed") == 0
    finally:
        await app.state.lilies_service.shutdown()


@pytest.mark.asyncio
async def test_real_daemon_asgi_cancel_terminal_events_drain_and_restart_recovery(
    tmp_path: Path,
) -> None:
    _, workflow, harness, auth = await platform_parts(tmp_path)
    settings = LiliesSettings(
        data_dir=tmp_path / "daemon",
        workspace_root=tmp_path / "daemon-workspaces",
        model="fixture-model",
        event_poll_seconds=0.01,
    )
    app = create_lilies_app(settings, provider=ImmediateProvider())
    await app.state.lilies_service.initialize()
    try:

        class ASGIBoundedEventClient(LocalLiliesHttpClient):
            # httpx's in-process ASGI transport buffers an infinite streaming
            # response.  Keep every mutation/receipt on the real ASGI API while
            # adapting only the bounded SSE read to the same durable store.
            fetch_calls = 0
            ack_calls = 0
            get_session_calls = 0

            async def fetch_events(
                self,
                _: str,
                __: str,
                session_id: str,
                *,
                after: int,
                max_events: int = 100,
                wait_seconds: float = 0.25,
            ) -> list[dict[str, Any]]:
                del wait_seconds
                self.fetch_calls += 1
                events = await app.state.lilies_storage.list_events(
                    session_id,
                    after=after,
                    limit=max_events,
                )
                return [
                    {
                        "seq": int(event["seq"]),
                        "event": str(event["event_type"]),
                        "data": dict(event["data"]),
                    }
                    for event in events
                ]

            async def acknowledge_events(
                self,
                base_url: str,
                access_token: str,
                session_id: str,
                payload: dict[str, Any],
            ) -> dict[str, Any]:
                self.ack_calls += 1
                return await super().acknowledge_events(base_url, access_token, session_id, payload)

            async def get_session(
                self,
                base_url: str,
                access_token: str,
                session_id: str,
            ) -> dict[str, Any]:
                self.get_session_calls += 1
                return await super().get_session(base_url, access_token, session_id)

        code = await app.state.lilies_storage.create_pairing_code(
            allowed_scopes=[
                "lilies.session:read",
                "lilies.session:write",
                "lilies.permission:resolve",
                "lilies.credential:write",
                "lilies.observability:read",
            ]
        )
        client = ASGIBoundedEventClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 43122))
        )
        bridge_path = tmp_path / "platform" / "local-lilies-bridge.db"
        bridge = LocalLiliesBridge(
            enabled=True,
            store=LocalLiliesBridgeStore(bridge_path),
            workflow_storage=workflow,
            harness=harness,
            auth_store=auth,
            client=client,
            platform_base_url="http://127.0.0.1:8001",
            contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        )
        await bridge.initialize()
        connection = await bridge.pair_connection(
            PairLocalLiliesRequest(
                idempotency_key="real-terminal-drain-pair-0001",
                base_url="http://127.0.0.1:8765",
                pairing_code=code["pairing_code"],
                expected_daemon_fingerprint=settings.daemon_fingerprint(),
            )
        )

        async def start(marker: str) -> LocalLiliesAssignment:
            application_id = await empty_application(workflow, marker)
            return await bridge.start_build(
                application_id,
                build_request(connection.connection_id, marker),
            )

        first = await start("real-terminal-drain")
        cancelled = await bridge.cancel_assignment(
            first.assignment_id,
            idempotency_key="real-terminal-drain-cancel-0001",
        )
        daemon_events = await app.state.lilies_storage.list_events(str(first.session_id))
        daemon_ack = await app.state.lilies_storage.get_ack(
            str(connection.client_id), str(first.session_id)
        )
        raw = await bridge.store.get_assignment(first.assignment_id)
        highest = max(int(event["seq"]) for event in daemon_events)
        assert cancelled.phase == BridgeAssignmentPhase.cancelled
        assert "assignment.cancelled" in {event["event_type"] for event in daemon_events}
        assert highest == raw["relay_cursor"] == raw["ack_cursor"]
        assert highest == daemon_ack["cursor"]
        assert raw["terminal_events_drained_at"] is not None
        assert (await workflow.get_build(str(first.build_id)))["status"] == "cancelled"
        call_marker = (client.fetch_calls, client.ack_calls, client.get_session_calls)
        replay = await bridge.relay_events(first.assignment_id)
        assert replay.assignment.phase == BridgeAssignmentPhase.cancelled
        assert (client.fetch_calls, client.ack_calls, client.get_session_calls) == call_marker
        assert (await bridge.recover_pending_assignments()).scanned == 0

        second = await start("real-terminal-restart")
        bridge.fault_hook = CrashOnce("terminal_relay.committed_before_ack")
        with pytest.raises(InjectedCrash, match="terminal_relay.committed_before_ack"):
            await bridge.cancel_assignment(
                second.assignment_id,
                idempotency_key="real-terminal-restart-cancel-0001",
            )
        committed = await bridge.store.get_assignment(second.assignment_id)
        assert committed["phase"] == "cancelled"
        assert committed["relay_cursor"] > committed["ack_cursor"]
        assert committed["terminal_events_drained_at"] is None

        restarted = LocalLiliesBridge(
            enabled=True,
            store=LocalLiliesBridgeStore(bridge_path),
            workflow_storage=workflow,
            harness=harness,
            auth_store=auth,
            client=client,
            platform_base_url="http://127.0.0.1:8001",
            contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        )
        await restarted.initialize()
        recovery = await restarted.recover_pending_assignments()
        final = await restarted.store.get_assignment(second.assignment_id)
        final_events = await app.state.lilies_storage.list_events(str(second.session_id))
        final_ack = await app.state.lilies_storage.get_ack(
            str(connection.client_id), str(second.session_id)
        )
        final_highest = max(int(event["seq"]) for event in final_events)
        assert recovery.scanned == 1
        assert recovery.cancelled == 1
        assert "assignment.cancelled" in {event["event_type"] for event in final_events}
        assert final_highest == final["relay_cursor"] == final["ack_cursor"]
        assert final_highest == final_ack["cursor"]
        assert final["terminal_events_drained_at"] is not None
        assert (await workflow.get_build(str(second.build_id)))["status"] == "cancelled"
        final_marker = (client.fetch_calls, client.ack_calls, client.get_session_calls)
        final_replay = await restarted.relay_events(second.assignment_id)
        assert final_replay.assignment.phase == BridgeAssignmentPhase.cancelled
        assert (client.fetch_calls, client.ack_calls, client.get_session_calls) == final_marker
        assert (await restarted.recover_pending_assignments()).scanned == 0
    finally:
        await app.state.lilies_service.shutdown()


@pytest.mark.asyncio
async def test_real_daemon_asgi_pair_assignment_idempotency_and_no_prebuilt_draft(
    tmp_path: Path,
) -> None:
    storage, workflow, harness, auth = await platform_parts(tmp_path)
    provider = ImmediateProvider()
    settings = LiliesSettings(
        data_dir=tmp_path / "daemon",
        workspace_root=tmp_path / "daemon-workspaces",
        model="fixture-model",
        event_poll_seconds=0.01,
    )
    app = create_lilies_app(settings, provider=provider)
    await app.state.lilies_service.initialize()
    try:
        code = await app.state.lilies_storage.create_pairing_code(
            allowed_scopes=[
                "lilies.session:read",
                "lilies.session:write",
                "lilies.permission:resolve",
                "lilies.credential:write",
                "lilies.observability:read",
            ]
        )
        client = LocalLiliesHttpClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 43120))
        )
        bridge = LocalLiliesBridge(
            enabled=True,
            store=LocalLiliesBridgeStore(tmp_path / "platform" / "local-lilies-bridge.db"),
            workflow_storage=workflow,
            harness=harness,
            auth_store=auth,
            client=client,
            platform_base_url="http://127.0.0.1:8001",
            contract_digest_provider=lambda _scopes, _apps, _actions: DIGEST,
        )
        await bridge.initialize()
        connection = await bridge.pair_connection(
            PairLocalLiliesRequest(
                idempotency_key="real-daemon-pair-0001",
                base_url="http://127.0.0.1:8765",
                pairing_code=code["pairing_code"],
                expected_daemon_fingerprint=settings.daemon_fingerprint(),
            )
        )
        application_id = await empty_application(workflow, "real")
        request = build_request(connection.connection_id, "real")
        assignment = await bridge.start_build(application_id, request)
        replay = await bridge.start_build(application_id, request)
        assert replay.assignment_id == assignment.assignment_id
        assert replay.session_id == assignment.session_id

        session = await app.state.lilies_storage.get_session(str(assignment.session_id))
        for _ in range(300):
            if session["status"] == "ready" and provider.calls == 1:
                break
            await asyncio.sleep(0.01)
            session = await app.state.lilies_storage.get_session(str(assignment.session_id))
        assert session["status"] == "ready" and provider.calls == 1, {
            "status": session["status"],
            "provider_calls": provider.calls,
            "events": await app.state.lilies_storage.list_events(str(assignment.session_id)),
        }
        refreshed = await bridge.resume_assignment(assignment.assignment_id)
        assert refreshed.phase == BridgeAssignmentPhase.running
        assert refreshed.status == "ready"
        assert provider.calls == 1
        assert (
            await bridge.get_assignment_by_build(assignment.build_id)
        ).application_id == application_id
        assert (
            await bridge.get_assignment_by_session(assignment.session_id)
        ).build_id == assignment.build_id

        draft = await workflow.get_draft(str(application_id))
        assert draft["revision"] == 0
        assert draft["snapshot"].workflow.nodes == []
        raw = await bridge.store.get_assignment(assignment.assignment_id)
        wire = json.loads(raw["submission_json"])
        assert wire["target"] == {
            "mode": "existing",
            "application_id": str(application_id),
        }
        assert "platform_application_create" not in wire["constraints"]["allowed_actions"]
        assert wire["constraints"]["max_turns"] == 80
        assert wire["constraints"]["max_tool_calls"] == 400
        assert wire["constraints"]["network_policy"] == "allowlist"
        assert wire["constraints"]["allowed_hosts"] == ["127.0.0.1"]
        assert "collaboration" not in wire

        daemon_secret = await bridge._resolve_secret(  # noqa: SLF001 - security assertion
            f"secret://local-lilies-connection:{connection.connection_id}/daemon-access-token"
        )
        assert_plaintext_absent(
            [storage.db_path, auth.db_path, bridge.store.db_path], daemon_secret
        )
    finally:
        await app.state.lilies_service.shutdown()
