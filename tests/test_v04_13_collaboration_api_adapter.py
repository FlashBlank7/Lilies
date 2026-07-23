from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient

from agent_platform.collaboration_api import install_collaboration_api


CHANNEL_ID = UUID("11111111-1111-4111-8111-111111111111")
CLAIM_ID = UUID("22222222-2222-4222-8222-222222222222")
REPORT_ID = UUID("33333333-3333-4333-8333-333333333333")


class FakeCollaborationError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def public_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": str(self)}


class FakeCollaborationService:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def require_enabled(self) -> None:
        if not self.enabled:
            raise FakeCollaborationError(
                "feature_disabled",
                "collaboration is disabled",
                404,
            )

    async def authenticate_lilies(
        self,
        access_token: str,
        *,
        channel_id: UUID,
        required_scope: str,
    ) -> dict[str, str]:
        if access_token != "channel-good-bearer" or channel_id != CHANNEL_ID:
            raise FakeCollaborationError("credential_denied", "credential denied", 403)
        return {
            "role": "lilies",
            "sender_id": "lilies-local",
            "scope": required_scope,
        }

    def authenticate_developer(
        self,
        access_token: str,
        *,
        required_scope: str,
    ) -> dict[str, str]:
        if access_token != "developer-good-bearer":
            raise FakeCollaborationError(
                "collaboration_not_found",
                "collaboration resource was not found",
                404,
            )
        return {
            "role": "codex",
            "sender_id": "developer-local",
            "scope": required_scope,
        }

    async def authenticate_verifier(
        self,
        access_token: str,
        *,
        claim_id: UUID,
        required_scope: str,
    ) -> dict[str, str]:
        if access_token != "verifier-good-bearer" or claim_id != CLAIM_ID:
            raise FakeCollaborationError("credential_denied", "credential denied", 403)
        return {
            "role": "verifier",
            "sender_id": "verifier-local",
            "scope": required_scope,
        }

    async def resolve_event_cursor(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        requested_after: int,
        durable: bool,
    ) -> int:
        self.calls.append(
            (
                "resolve_event_cursor",
                {
                    "principal": principal,
                    "channel_id": channel_id,
                    "requested_after": requested_after,
                    "durable": durable,
                },
            )
        )
        return 3 if durable else requested_after

    async def list_events(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        after: int,
        limit: int,
        history_replay: bool = False,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "list_events",
                {
                    "principal": principal,
                    "channel_id": channel_id,
                    "after": after,
                    "limit": limit,
                    "history_replay": history_replay,
                },
            )
        )
        return [
            {
                "message_id": "44444444-4444-4444-8444-444444444444",
                "channel_id": str(channel_id),
                "seq": 4,
                "message_type": "developer_response",
                "payload": {"outcome": "implemented"},
            },
            # Global seq=5 may be invisible to Lilies.  A visible projection is
            # monotonic but need not be contiguous.
            {
                "message_id": "66666666-6666-4666-8666-666666666666",
                "channel_id": str(channel_id),
                "seq": 6,
                "message_type": "control",
                "payload": {"kind": "report_status_changed"},
            },
        ][:limit]

    async def ack_events(
        self,
        *,
        principal: Any,
        channel_id: UUID,
        request: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "ack_events",
                {"principal": principal, "channel_id": channel_id, "request": request},
            )
        )
        return request.model_dump(mode="json")

    async def list_channels(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_channels", kwargs))
        return [{"channel_id": str(CHANNEL_ID), "status": "active"}]

    async def developer_inbox(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("developer_inbox", kwargs))
        return {"items": [], "pending_user_action": True}


def build_app(service: FakeCollaborationService) -> FastAPI:
    app = FastAPI()
    bearer = HTTPBearer(auto_error=False)

    async def require_user_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        del request
        if credentials is None or credentials.credentials != "user-good-bearer":
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_api_token", "message": "invalid API token"},
            )

    install_collaboration_api(
        app,
        service,
        require_user_token=require_user_token,
    )
    return app


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_feature_off_is_a_uniform_404_before_body_or_auth_disclosure() -> None:
    client = TestClient(build_app(FakeCollaborationService(enabled=False)))

    malformed = client.post(
        f"/api/v1/collaboration/channels/{CHANNEL_ID}/reports",
        json={"unexpected": "value"},
    )
    studio = client.get("/api/v1/studio/collaboration/channels")
    developer = client.get("/api/v1/developer/collaboration/inbox")

    for response in (malformed, studio, developer):
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


def test_lilies_auth_is_header_only_and_hides_bearer_and_channel_failures() -> None:
    client = TestClient(build_app(FakeCollaborationService()))
    path = f"/api/v1/collaboration/channels/{CHANNEL_ID}/events?format=json"

    missing = client.get(path)
    wrong = client.get(path, headers=auth("wrong-channel-bearer"))
    wrong_channel = client.get(
        "/api/v1/collaboration/channels/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/events"
        "?format=json",
        headers=auth("channel-good-bearer"),
    )
    malformed_channel = client.get(
        "/api/v1/collaboration/channels/not-a-uuid/events?format=json",
        headers=auth("wrong-channel-bearer"),
    )
    query_secret = client.get(f"{path}&ToKeN=channel-good-bearer")

    for response in (missing, wrong, wrong_channel, malformed_channel, query_secret):
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}


def test_unauthorized_route_probes_match_an_unknown_path_without_redirect_or_405() -> None:
    client = TestClient(build_app(FakeCollaborationService()), follow_redirects=False)
    exact = "/api/v1/developer/collaboration/inbox"
    unknown = "/api/v1/developer/collaboration/not-a-real-endpoint"
    probes = (
        ("GET", exact, {}),
        ("GET", f"{exact}/", auth("wrong-bearer")),
        ("POST", exact, auth("wrong-bearer")),
        ("HEAD", exact, auth("wrong-bearer")),
        ("OPTIONS", exact, auth("wrong-bearer")),
        ("GET", exact, auth("wrong-bearer")),
    )

    for method, path, headers in probes:
        baseline = client.request(method, unknown, headers=headers)
        response = client.request(method, path, headers=headers)
        assert baseline.status_code == 404
        assert response.status_code == baseline.status_code
        assert response.content == baseline.content
        assert "location" not in response.headers


def test_lilies_json_events_are_bounded_and_resume_from_durable_ack() -> None:
    service = FakeCollaborationService()
    client = TestClient(build_app(service))

    response = client.get(
        f"/api/v1/collaboration/channels/{CHANNEL_ID}/events"
        "?format=json&after=99&limit=2",
        headers={**auth("channel-good-bearer"), "Last-Event-ID": "120"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "channel_id": str(CHANNEL_ID),
        "after": 3,
        "next_cursor": 6,
        "events": [
            {
                "message_id": "44444444-4444-4444-8444-444444444444",
                "channel_id": str(CHANNEL_ID),
                "seq": 4,
                "message_type": "developer_response",
                "payload": {"outcome": "implemented"},
            },
            {
                "message_id": "66666666-6666-4666-8666-666666666666",
                "channel_id": str(CHANNEL_ID),
                "seq": 6,
                "message_type": "control",
                "payload": {"kind": "report_status_changed"},
            },
        ],
    }
    cursor_call = next(item for item in service.calls if item[0] == "resolve_event_cursor")
    assert cursor_call[1]["requested_after"] == 120
    assert cursor_call[1]["durable"] is True
    list_call = next(item for item in service.calls if item[0] == "list_events")
    assert list_call[1]["after"] == 3
    assert list_call[1]["limit"] == 2


def test_lilies_json_history_replay_can_read_before_durable_ack() -> None:
    service = FakeCollaborationService()
    client = TestClient(build_app(service))

    response = client.get(
        f"/api/v1/collaboration/channels/{CHANNEL_ID}/events"
        "?format=json&after=0&limit=2&history_replay=true",
        headers={**auth("channel-good-bearer"), "Last-Event-ID": "120"},
    )

    assert response.status_code == 200
    assert response.json()["after"] == 0
    assert response.json()["history_replay"] is True
    cursor_call = next(
        item for item in service.calls if item[0] == "resolve_event_cursor"
    )
    assert cursor_call[1]["requested_after"] == 0
    assert cursor_call[1]["durable"] is False
    list_call = next(item for item in service.calls if item[0] == "list_events")
    assert list_call[1]["after"] == 0
    assert list_call[1]["limit"] == 2


def test_ack_uses_strict_model_and_validation_never_echoes_input_values() -> None:
    service = FakeCollaborationService()
    client = TestClient(build_app(service))
    path = f"/api/v1/collaboration/channels/{CHANNEL_ID}/acks"
    valid = {
        "idempotency_key": "collaboration.ack.0001",
        "expected_cursor_revision": 0,
        "reader_role": "lilies",
        "reader_id": "lilies-local",
        "ack_seq": 6,
    }

    accepted = client.post(path, headers=auth("channel-good-bearer"), json=valid)
    rejected = client.post(
        path,
        headers=auth("channel-good-bearer"),
        json={**valid, "token": "do-not-echo-this-secret"},
    )

    assert accepted.status_code == 200
    assert accepted.json()["ack_seq"] == 6
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "invalid_collaboration_request"
    assert "do-not-echo-this-secret" not in rejected.text


def test_studio_and_developer_use_distinct_header_credentials() -> None:
    service = FakeCollaborationService()
    client = TestClient(build_app(service))

    assert client.get("/api/v1/studio/collaboration/channels").status_code == 404
    studio = client.get(
        "/api/v1/studio/collaboration/channels",
        headers=auth("user-good-bearer"),
    )
    developer_with_user = client.get(
        "/api/v1/developer/collaboration/inbox",
        headers=auth("user-good-bearer"),
    )
    developer = client.get(
        "/api/v1/developer/collaboration/inbox",
        headers=auth("developer-good-bearer"),
    )

    assert studio.status_code == 200
    assert studio.json()[0]["channel_id"] == str(CHANNEL_ID)
    assert developer_with_user.status_code == 404
    assert developer.status_code == 200
    assert developer.json() == {"items": [], "pending_user_action": True}


def test_route_surface_contains_all_role_and_causal_export_endpoints() -> None:
    app = build_app(FakeCollaborationService())
    paths = {getattr(route, "path", "") for route in app.routes}

    assert {
        "/api/v1/collaboration/channels/{channel_id}/reports",
        "/api/v1/collaboration/channels/{channel_id}/reports/{report_id}/revisions",
        "/api/v1/collaboration/channels/{channel_id}/reports/{report_id}/evidence",
        "/api/v1/collaboration/channels/{channel_id}/reports/{report_id}/reprobes",
        "/api/v1/collaboration/channels/{channel_id}/events",
        "/api/v1/collaboration/channels/{channel_id}/acks",
        "/api/v1/collaboration/channels/{channel_id}/verification-claims",
        "/api/v1/studio/collaboration/channels",
        "/api/v1/studio/collaboration/channels/{channel_id}",
        "/api/v1/studio/collaboration/channels/{channel_id}/events",
        "/api/v1/studio/collaboration/channels/{channel_id}/export",
        "/api/v1/studio/collaboration/reports/{report_id}/decision",
        "/api/v1/studio/collaboration/channels/{channel_id}/settings",
        "/api/v1/studio/collaboration/channels/{channel_id}/close",
        "/api/v1/developer/collaboration/inbox",
        "/api/v1/developer/collaboration/reports/{report_id}/lease",
        "/api/v1/developer/collaboration/reports/{report_id}/lease/renew",
        "/api/v1/developer/collaboration/reports/{report_id}/lease/release",
        "/api/v1/developer/collaboration/reports/{report_id}/responses",
        "/api/v1/developer/collaboration/reports/{report_id}/task-amendments",
        "/api/v1/developer/collaboration/reports/{report_id}/environment-responses",
        "/api/v1/developer/collaboration/claims/{claim_id}/verification-results",
    } <= paths
