from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.local_lilies_bridge import (
    BridgeAssignmentPhase,
    BridgeConnectionStatus,
    BridgeDesiredState,
    LocalLiliesAssignment,
    LocalLiliesBridgeUnavailable,
    LocalLiliesConnection,
    LocalLiliesRecoverySummary,
    LocalLiliesRelayEvent,
    LocalLiliesRelayResult,
)
from agent_platform.local_lilies_bridge_api import (
    _assignment_event_stream,
    install_local_lilies_bridge_api,
)
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from tests.test_runtime import ScriptedProvider


ZERO_DIGEST = "sha256:" + "0" * 64
FINGERPRINT = "sha256:" + "a" * 64
PLATFORM_SCOPES = (
    PlatformBlackboxScope.catalog_read,
    PlatformBlackboxScope.application_write,
    PlatformBlackboxScope.draft_write,
    PlatformBlackboxScope.test_execute,
    PlatformBlackboxScope.run_execute,
    PlatformBlackboxScope.trace_read,
    PlatformBlackboxScope.artifact_read,
)


def _settings(
    tmp_path: Path,
    *,
    enabled: bool = False,
    default_route: bool = False,
) -> Settings:
    return Settings(
        api_token="platform-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3_600,
        lilies_local_agent_enabled=enabled,
        lilies_local_builder_default=default_route,
        lilies_local_discovery_file=tmp_path / "daemon.json",
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer platform-test-token"}


def _connection(connection_id: UUID) -> LocalLiliesConnection:
    now = datetime.now(timezone.utc)
    return LocalLiliesConnection(
        connection_id=connection_id,
        base_url="http://127.0.0.1:8765",
        daemon_fingerprint=FINGERPRINT,
        client_id=uuid4(),
        granted_scopes=[
            "lilies.session:read",
            "lilies.session:write",
            "lilies.credential:write",
        ],
        expires_at=now + timedelta(days=1),
        status=BridgeConnectionStatus.connected,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


def _assignment(
    *,
    connection_id: UUID,
    application_id: UUID,
    assignment_id: UUID | None = None,
    build_id: UUID | None = None,
    session_id: UUID | None = None,
    relay_cursor: int = 0,
) -> LocalLiliesAssignment:
    now = datetime.now(timezone.utc)
    return LocalLiliesAssignment(
        assignment_id=assignment_id or uuid4(),
        application_id=application_id,
        build_id=build_id or uuid4(),
        session_id=session_id or uuid4(),
        connection_id=connection_id,
        phase=BridgeAssignmentPhase.running,
        status="running",
        desired_state=BridgeDesiredState.active,
        daemon_status="running",
        relay_cursor=relay_cursor,
        ack_cursor=relay_cursor,
        created_at=now,
        updated_at=now,
    )


def _build_payload(connection_id: UUID, marker: str = "001") -> dict[str, Any]:
    return {
        "idempotency_key": f"platform-build-{marker}-0001",
        "connection_id": str(connection_id),
        "requirement": (
            "Build an auditable enterprise document-review workflow with human escalation."
        ),
        "business_context": {
            "customer_roles": ["operations reviewer"],
            "business_goal": "Review business documents with traceable decisions.",
            "inputs": ["incoming documents"],
            "outputs": ["review decision"],
            "constraints": ["ambiguous cases require human review"],
        },
        "deliverables": [
            {
                "name": "review workflow",
                "description": "Editable workflow and acceptance evidence",
                "media_type": "application/vnd.lilies.workflow+json",
                "required": True,
            }
        ],
    }


def test_disabled_route_exposes_safe_discovery_status_and_rejects_query_secrets(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        disabled = client.get("/api/v1/local-lilies/status", headers=_auth())
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["connections"] == []
        assert disabled.json()["discovery"]["status"] == "unavailable"

        malformed_still_disabled = client.post(
            "/api/v1/local-lilies/connections",
            headers=_auth(),
            json={"unknown": "value"},
        )
        assert malformed_still_disabled.status_code == 404

        for query_key in (
            "FRONTEND_TOKEN",
            "pairing_code",
            "previous_access_token",
            "prepared_access_token",
            "bootstrap_credential",
        ):
            query_secret = client.get(
                f"/api/v1/local-lilies/status?{query_key}=QUERY-SECRET-MARKER"
            )
            assert query_secret.status_code == 400
            assert query_secret.json()["detail"]["code"] == "query_secret_forbidden"
            assert "QUERY-SECRET-MARKER" not in query_secret.text


def test_validation_errors_never_echo_pairing_codes_or_tokens(
    tmp_path: Path,
    caplog: Any,
) -> None:
    app = create_app(_settings(tmp_path, enabled=True), ScriptedProvider())
    app.state.services.local_lilies_bridge.recover_pending_assignments = AsyncMock(
        return_value=LocalLiliesRecoverySummary(
            scanned=0,
            recovered=0,
            waiting=0,
            cancelled=0,
            unavailable=0,
            failed=0,
        )
    )
    pairing_secret = "PAIRING-CODE-MUST-NOT-ECHO"
    bearer_secret = "BODY-TOKEN-MUST-NOT-ECHO"
    caplog.set_level(logging.INFO)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/local-lilies/connections",
            headers=_auth(),
            json={
                "idempotency_key": "short",
                "base_url": "http://127.0.0.1:8765",
                "pairing_code": pairing_secret,
                "expected_daemon_fingerprint": FINGERPRINT,
                "token": bearer_secret,
            },
        )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_local_lilies_request"
    assert detail["errors"]
    assert all(set(item) == {"loc", "msg", "type"} for item in detail["errors"])
    serialized = response.text + caplog.text
    assert pairing_secret not in serialized
    assert bearer_secret not in serialized


def test_independent_verification_route_requires_completion_and_calls_provider() -> None:
    connection_id = uuid4()
    application_id = uuid4()
    assignment_id = uuid4()
    assignment = _assignment(
        connection_id=connection_id,
        application_id=application_id,
        assignment_id=assignment_id,
    )

    class FakeBridge:
        def require_enabled(self) -> None:
            return

        async def get_assignment(self, _assignment_id: UUID) -> LocalLiliesAssignment:
            return assignment

    provider = AsyncMock(
        return_value={
            "schema_version": "1.0",
            "assignment_id": str(assignment_id),
            "claim_status": "independently_verified",
        }
    )

    async def require_token() -> None:
        return

    app = FastAPI()
    install_local_lilies_bridge_api(
        app,
        FakeBridge(),  # type: ignore[arg-type]
        require_token=require_token,
        formal_verification_provider=provider,
    )
    with TestClient(app) as client:
        incomplete = client.post(
            f"/api/v1/local-lilies/assignments/{assignment_id}/"
            "independent-verification"
        )
        assert incomplete.status_code == 409
        assert incomplete.json()["detail"]["code"] == (
            "formal_assignment_not_completed"
        )

        assignment = assignment.model_copy(
            update={
                "phase": BridgeAssignmentPhase.completed,
                "status": "verification_pending",
            }
        )
        completed = client.post(
            f"/api/v1/local-lilies/assignments/{assignment_id}/"
            "independent-verification"
        )

    assert completed.status_code == 200
    assert completed.json()["claim_status"] == "independently_verified"
    provider.assert_awaited_once_with(assignment_id)


def test_default_route_flag_is_wired_only_when_local_agent_is_enabled(tmp_path: Path) -> None:
    enabled_app = create_app(
        _settings(tmp_path / "enabled", enabled=True, default_route=True),
        ScriptedProvider(),
    )
    enabled_app.state.services.local_lilies_bridge.recover_pending_assignments = AsyncMock(
        return_value=LocalLiliesRecoverySummary(
            scanned=0,
            recovered=0,
            waiting=0,
            cancelled=0,
            unavailable=0,
            failed=0,
        )
    )
    with TestClient(enabled_app) as client:
        response = client.get("/api/v1/local-lilies/status", headers=_auth())
        assert response.status_code == 200
        assert response.json()["default_route"] is True

    disabled_app = create_app(
        _settings(tmp_path / "disabled", enabled=False, default_route=True),
        ScriptedProvider(),
    )
    assert disabled_app.state.services.local_lilies_bridge.default_route is False


def test_platform_routes_cover_pairing_assignment_lookup_actions_and_safe_errors(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path, enabled=True), ScriptedProvider())
    bridge = app.state.services.local_lilies_bridge
    bridge.recover_pending_assignments = AsyncMock(
        return_value=LocalLiliesRecoverySummary(
            scanned=0,
            recovered=0,
            waiting=0,
            cancelled=0,
            unavailable=0,
            failed=0,
        )
    )
    connection_id = uuid4()
    application_id = uuid4()
    connection = _connection(connection_id)
    assignment = _assignment(connection_id=connection_id, application_id=application_id)
    status_payload = {
        "enabled": True,
        "default_route": False,
        "connections": [connection.model_dump(mode="json")],
    }

    with TestClient(app) as client:
        bridge.status = AsyncMock(return_value=status_payload)
        bridge.pair_connection = AsyncMock(return_value=connection)
        bridge.list_connections = AsyncMock(return_value=[connection])
        bridge.get_connection = AsyncMock(return_value=connection)
        bridge.refresh_connection = AsyncMock(return_value=connection)
        bridge.reconnect_connection = AsyncMock(return_value=connection)
        bridge.start_build = AsyncMock(return_value=assignment)
        bridge.list_assignments_for_application = AsyncMock(return_value=[assignment])
        bridge.get_assignment = AsyncMock(return_value=assignment)
        bridge.get_assignment_by_build = AsyncMock(return_value=assignment)
        bridge.get_assignment_by_session = AsyncMock(return_value=assignment)
        bridge.resume_assignment = AsyncMock(return_value=assignment)
        bridge.cancel_assignment = AsyncMock(return_value=assignment)
        bridge.relay_events = AsyncMock(
            return_value=LocalLiliesRelayResult(
                assignment=assignment,
                inserted=0,
                replayed=0,
                relay_cursor=0,
                ack_cursor=0,
            )
        )

        paired = client.post(
            "/api/v1/local-lilies/connections",
            headers=_auth(),
            json={
                "idempotency_key": "platform-pairing-0001",
                "base_url": "http://127.0.0.1:8765",
                "pairing_code": "PAIR-CODE-001",
                "expected_daemon_fingerprint": FINGERPRINT,
            },
        )
        assert paired.status_code == 200
        assert paired.json()["connections"][0]["connection_id"] == str(connection_id)
        assert client.get(
            "/api/v1/local-lilies/connections", headers=_auth()
        ).status_code == 200
        assert client.get(
            f"/api/v1/local-lilies/connections/{connection_id}", headers=_auth()
        ).status_code == 200
        assert client.post(
            f"/api/v1/local-lilies/connections/{connection_id}/refresh", headers=_auth()
        ).status_code == 200
        assert client.post(
            f"/api/v1/local-lilies/connections/{connection_id}/reconnect",
            headers=_auth(),
            json={
                "idempotency_key": "platform-reconnect-0001",
                "pairing_code": "PAIR-CODE-002",
            },
        ).status_code == 200

        started = client.post(
            f"/api/v1/local-lilies/applications/{application_id}/builds",
            headers=_auth(),
            json=_build_payload(connection_id),
        )
        assert started.status_code == 200
        assert started.json()["assignment_id"] == str(assignment.assignment_id)
        assert started.json()["application_id"] == str(application_id)

        routes = (
            f"/api/v1/local-lilies/applications/{application_id}/assignments",
            f"/api/v1/local-lilies/assignments/{assignment.assignment_id}",
            f"/api/v1/local-lilies/builds/{assignment.build_id}",
            f"/api/v1/local-lilies/sessions/{assignment.session_id}",
        )
        for route in routes:
            response = client.get(route, headers=_auth())
            assert response.status_code == 200, response.text

        assert client.post(
            f"/api/v1/local-lilies/assignments/{assignment.assignment_id}/resume",
            headers=_auth(),
        ).status_code == 200
        assert client.post(
            f"/api/v1/local-lilies/assignments/{assignment.assignment_id}/cancel",
            headers=_auth(),
        ).status_code == 200
        cancel_kwargs = bridge.cancel_assignment.await_args.kwargs
        assert cancel_kwargs["idempotency_key"] == (
            f"platform.cancel.{assignment.assignment_id.hex}"
        )
        assert client.post(
            f"/api/v1/local-lilies/assignments/{assignment.assignment_id}/relay",
            headers=_auth(),
            json={"max_events": 25},
        ).status_code == 200
        assert bridge.relay_events.await_args.kwargs["max_events"] == 25

        unavailable = LocalLiliesBridgeUnavailable(
            "local Lilies daemon is unavailable",
            details={
                "application_id": str(application_id),
                "assignment_id": str(assignment.assignment_id),
                "build_id": str(assignment.build_id),
                "session_id": str(assignment.session_id),
                "status": "unavailable",
            },
        )
        bridge.start_build.side_effect = unavailable
        failed = client.post(
            f"/api/v1/local-lilies/applications/{application_id}/builds",
            headers=_auth(),
            json=_build_payload(connection_id, "002"),
        )
        assert failed.status_code == 503
        assert failed.json()["detail"] == unavailable.public_detail()


def test_lifespan_recovery_is_managed_and_does_not_block_startup(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enabled=True), ScriptedProvider())
    bridge = app.state.services.local_lilies_bridge
    entered = asyncio.Event()

    async def blocked_recovery() -> LocalLiliesRecoverySummary:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    bridge.recover_pending_assignments = AsyncMock(side_effect=blocked_recovery)
    started_at = time.monotonic()
    recovery_task: asyncio.Task[Any] | None = None
    with TestClient(app) as client:
        assert time.monotonic() - started_at < 0.5
        client.portal.call(asyncio.wait_for, entered.wait(), 1)
        recovery_task = next(
            task
            for task in app.state.services.background_tasks
            if task.get_name() == "local-lilies-startup-recovery"
        )
        assert not recovery_task.done()
        assert app.state.local_lilies_recovery == {"status": "scheduled"}
        assert client.get("/health").status_code == 200

    assert recovery_task is not None
    assert recovery_task.cancelled()
    assert app.state.local_lilies_recovery == {"status": "cancelled"}


def test_assignment_event_proxy_uses_persisted_cursor_and_safe_unavailable_response(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path, enabled=True), ScriptedProvider())
    bridge = app.state.services.local_lilies_bridge
    bridge.recover_pending_assignments = AsyncMock(
        return_value=LocalLiliesRecoverySummary(
            scanned=0,
            recovered=0,
            waiting=0,
            cancelled=0,
            unavailable=0,
            failed=0,
        )
    )
    connection_id = uuid4()
    application_id = uuid4()
    assignment = _assignment(
        connection_id=connection_id,
        application_id=application_id,
        relay_cursor=3,
    )
    unavailable = LocalLiliesBridgeUnavailable(
        "local Lilies daemon is unavailable",
        details={
            "application_id": str(application_id),
            "assignment_id": str(assignment.assignment_id),
            "build_id": str(assignment.build_id),
            "session_id": str(assignment.session_id),
            "status": "unavailable",
        },
    )

    with TestClient(app) as client:
        bridge.get_assignment = AsyncMock(return_value=assignment)
        bridge.list_events = AsyncMock(return_value=[])
        bridge.relay_events = AsyncMock(side_effect=unavailable)

        invalid = client.get(
            f"/api/v1/local-lilies/assignments/{assignment.assignment_id}/events",
            headers={**_auth(), "Last-Event-ID": "cursor-secret"},
        )
        assert invalid.status_code == 400
        assert invalid.json()["detail"]["code"] == "invalid_event_cursor"

        failed = client.get(
            f"/api/v1/local-lilies/assignments/{assignment.assignment_id}/events?after=1",
            headers={**_auth(), "Last-Event-ID": "2"},
        )
        assert failed.status_code == 503
        assert failed.json()["detail"] == unavailable.public_detail()
        bridge.list_events.assert_awaited_with(assignment.assignment_id, after=2)


def test_assignment_event_stream_marks_replay_and_live_events_without_secret_cursor() -> None:
    assignment_id = uuid4()
    session_id = uuid4()
    now = datetime.now(timezone.utc)
    replayed = LocalLiliesRelayEvent(
        assignment_id=assignment_id,
        session_id=session_id,
        daemon_seq=3,
        event_type="tool.completed",
        data={"tool": "platform_draft_apply"},
        received_at=now,
    )
    live = LocalLiliesRelayEvent(
        assignment_id=assignment_id,
        session_id=session_id,
        daemon_seq=4,
        event_type="turn.completed",
        data={"status": "ready"},
        received_at=now,
    )

    class RequestFixture:
        async def is_disconnected(self) -> bool:
            return False

    class BridgeFixture:
        def __init__(self) -> None:
            self.list_calls = 0

        async def relay_events(self, *_: Any, **__: Any) -> None:
            return None

        async def list_events(self, *_: Any, **__: Any) -> list[LocalLiliesRelayEvent]:
            self.list_calls += 1
            return [live] if self.list_calls == 1 else []

    async def collect() -> tuple[str, str]:
        stream = _assignment_event_stream(
            BridgeFixture(),  # type: ignore[arg-type]
            RequestFixture(),  # type: ignore[arg-type]
            assignment_id,
            cursor=2,
            replay_boundary=3,
            initial_events=[replayed],
            poll_seconds=0.01,
        )
        try:
            return await anext(stream), await anext(stream)
        finally:
            await stream.aclose()

    first, second = asyncio.run(collect())
    first_payload = json.loads(first.split("data: ", 1)[1])
    second_payload = json.loads(second.split("data: ", 1)[1])
    assert first.startswith("id: 3\nevent: tool.completed\n")
    assert first_payload["replayed"] is True
    assert first_payload["event_id"] == f"{assignment_id}:3"
    assert second.startswith("id: 4\nevent: turn.completed\n")
    assert second_payload["replayed"] is False
    assert "token" not in first.casefold() + second.casefold()


def test_assignment_digest_uses_the_published_blackbox_contract_facade(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, enabled=True), ScriptedProvider())
    bridge = app.state.services.local_lilies_bridge
    bridge.recover_pending_assignments = AsyncMock(
        return_value=LocalLiliesRecoverySummary(
            scanned=0,
            recovered=0,
            waiting=0,
            cancelled=0,
            unavailable=0,
            failed=0,
        )
    )
    application_id = uuid4()
    assignment_id = uuid4()
    session_id = uuid4()
    with TestClient(app) as client:
        assignment_digest = client.portal.call(
            bridge.contract_digest_provider,
            PLATFORM_SCOPES,
            (application_id,),
        )
        issued = client.portal.call(
            app.state.services.platform_blackbox_auth.issue_credential,
            TaskCredentialGrant(
                assignment_id=assignment_id,
                session_id=session_id,
                scopes=list(PLATFORM_SCOPES),
                application_ids=[application_id],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        response = client.get(
            "/api/v1/lilies/platform-contract",
            headers={
                "Authorization": f"Bearer {issued.access_token.get_secret_value()}",
                "X-Lilies-Assignment-ID": str(assignment_id),
                "X-Lilies-Session-ID": str(session_id),
                "X-Lilies-Tool-Call-ID": "tool-contract-bootstrap-0001",
                "X-Lilies-Idempotency-Key": "contract-bootstrap-0001",
                "X-Lilies-Contract-Digest": ZERO_DIGEST,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["contract_digest"] == assignment_digest
