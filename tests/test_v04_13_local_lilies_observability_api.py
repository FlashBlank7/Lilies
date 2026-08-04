from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from agent_platform.local_lilies_bridge import (
    LocalLiliesBridgeDaemonRejected,
    LocalLiliesObservabilitySnapshot,
    LocalLiliesObservabilityUnavailable,
)
from agent_platform.local_lilies_bridge_api import install_local_lilies_bridge_api


FINGERPRINT = "sha256:" + "e" * 64


def _snapshot() -> LocalLiliesObservabilitySnapshot:
    return LocalLiliesObservabilitySnapshot.model_validate(
        {
            "schema_version": "1.0",
            "scope": "daemon_global",
            "coverage_complete": True,
            "daemon_fingerprint": FINGERPRINT,
            "daemon_instance_id": "d5a387ad-a687-4a47-8674-96583808d2e8",
            "captured_at": "2026-07-26T12:00:00+00:00",
            "activity_revision": 4,
            "model_egress_enabled": False,
            "max_session_tokens": 1_000_000,
            "usage": {
                "ledger_cursor": 1,
                "attempted_calls": 1,
                "recorded_calls": 1,
                "unknown_calls": 0,
                "input_tokens": 5,
                "output_tokens": 3,
                "total_tokens": 8,
                "cost_usd": 0.001,
            },
            "runtime": {
                "active_sessions": 0,
                "active_model_turns": 0,
                "active_provider_calls": 0,
                "active_development_model_calls": 0,
            },
            "startup": {
                "recovery_completed": True,
                "automatic_resume_policy": "explicit_request_only",
                "automatic_model_resume_count": 0,
                "explicit_resume_candidate_count": 0,
                "interrupted_sessions": 0,
                "interrupted_turns": 0,
                "interrupted_development_assignments": 0,
                "reconciliation_required_development_invocations": 0,
                "unreaped_development_processes": 0,
            },
        }
    )


class FakeBridge:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[UUID] = []
        self.error: Exception | None = None

    def require_enabled(self) -> None:
        return

    async def observability_snapshot(
        self,
        connection_id: UUID,
    ) -> LocalLiliesObservabilitySnapshot:
        self.calls.append(connection_id)
        if self.error is not None:
            raise self.error
        return _snapshot()


async def _require_token(
    authorization: str | None = Header(default=None),
) -> None:
    if authorization != "Bearer platform-observability-token":
        raise HTTPException(status_code=401, detail="authentication required")


def test_observability_proxy_requires_platform_token_and_publishes_strict_schema() -> None:
    bridge = FakeBridge()
    app = FastAPI()
    install_local_lilies_bridge_api(
        app,
        bridge,  # type: ignore[arg-type]
        require_token=_require_token,
    )
    connection_id = uuid4()
    path = (
        f"/api/v1/local-lilies/connections/{connection_id}/observability-snapshot"
    )

    with TestClient(app) as client:
        unauthenticated = client.get(path)
        authenticated = client.get(
            path,
            headers={"Authorization": "Bearer platform-observability-token"},
        )
        openapi: dict[str, Any] = client.get("/openapi.json").json()
        schema: dict[str, Any] = openapi["paths"][
            "/api/v1/local-lilies/connections/{connection_id}/observability-snapshot"
        ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        bridge.error = LocalLiliesObservabilityUnavailable(
            "legacy observability unavailable",
            details={"availability": "unknown"},
        )
        legacy_unknown = client.get(
            path,
            headers={"Authorization": "Bearer platform-observability-token"},
        )
        bridge.error = LocalLiliesBridgeDaemonRejected(
            "paired daemon receipt rejected",
        )
        daemon_rejected = client.get(
            path,
            headers={"Authorization": "Bearer platform-observability-token"},
        )
        invalid_identifier = client.get(
            "/api/v1/local-lilies/connections/not-a-uuid/observability-snapshot",
            headers={"Authorization": "Bearer platform-observability-token"},
        )
        redirect = client.get(
            path + "/",
            headers={"Authorization": "Bearer platform-observability-token"},
            follow_redirects=False,
        )

    bridge.error = RuntimeError("credential-bearing internal detail")
    with TestClient(app, raise_server_exceptions=False) as client:
        internal_error = client.get(
            path,
            headers={"Authorization": "Bearer platform-observability-token"},
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["cache-control"] == "no-store"
    assert authenticated.status_code == 200
    assert authenticated.headers["cache-control"] == "no-store"
    assert legacy_unknown.status_code == 409
    assert legacy_unknown.headers["cache-control"] == "no-store"
    assert daemon_rejected.status_code == 502
    assert daemon_rejected.headers["cache-control"] == "no-store"
    assert invalid_identifier.status_code == 422
    assert invalid_identifier.headers["cache-control"] == "no-store"
    assert redirect.status_code == 307
    assert redirect.headers["cache-control"] == "no-store"
    assert internal_error.status_code == 500
    assert internal_error.headers["cache-control"] == "no-store"
    assert internal_error.json() == {
        "detail": {
            "code": "local_lilies_observability_internal_error",
            "message": "local Lilies observability request failed",
        }
    }
    assert "credential-bearing internal detail" not in internal_error.text
    assert authenticated.json()["scope"] == "daemon_global"
    assert authenticated.json()["daemon_fingerprint"] == FINGERPRINT
    assert authenticated.json()["max_session_tokens"] == 1_000_000
    assert schema == {"$ref": "#/components/schemas/LocalLiliesObservabilitySnapshot"}
    snapshot_schema = openapi["components"]["schemas"][
        "LocalLiliesObservabilitySnapshot"
    ]
    max_session_schema = snapshot_schema["properties"]["max_session_tokens"]
    assert {
        "type": "integer",
        "minimum": 0.0,
        "maximum": float(4_000_000_000),
    } in max_session_schema["anyOf"]
    usage_schema = openapi["components"]["schemas"]["LocalLiliesObservabilityUsage"]
    assert usage_schema["properties"]["attempted_calls"]["maximum"] == float(2**63 - 1)
    assert usage_schema["properties"]["input_tokens"]["maximum"] == float(2**63 - 1)
    assert usage_schema["properties"]["cost_usd"]["maximum"] == 1_000_000_000_000.0
    assert bridge.calls == [connection_id, connection_id, connection_id, connection_id]
