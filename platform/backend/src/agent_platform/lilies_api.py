from __future__ import annotations

import asyncio
import hmac
import inspect
import ipaddress
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .lilies_config import LiliesSettings
from .lilies_models import (
    AssignmentSubmissionResult,
    BuildAssignment,
    CredentialProvisionRequest,
    CredentialProvisionResult,
    CredentialRevokeRequest,
    CredentialRevokeResult,
    DaemonHealth,
    DaemonStatus,
    DaemonStopRequest,
    DaemonStopResult,
    FormalWorkspaceStagingReceipt,
    FormalWorkspaceStagingRequest,
    LocalScope,
    PairingCodeCreateRequest,
    PairingCodeResult,
    PairingExchangeRequest,
    PairingExchangeResult,
    PermissionDecisionRequest,
    PermissionDecisionResult,
    SessionAckRequest,
    SessionAckResult,
    SessionCancelRequest,
    SessionCreateRequest,
    SessionListResult,
    SessionMessageRequest,
    SessionOperationResult,
    SessionResult,
    SessionResumeRequest,
)
from .lilies_storage import (
    LiliesAccessDeniedError,
    LiliesAuthenticationError,
    LiliesConflictError,
    LiliesNotFoundError,
    LiliesStorage,
)
from .providers.base import ModelProvider


ClientRecord = dict[str, Any]
StopCallback = Callable[[], Awaitable[None] | None]


def _build_service(
    settings: LiliesSettings,
    storage: LiliesStorage,
    provider: ModelProvider | None,
) -> Any:
    # Kept behind this small seam so API contract tests do not need a live model.
    from .lilies_service import build_local_lilies_core

    return build_local_lilies_core(
        settings,
        storage=storage,
        provider=provider,
    ).service


def _is_loopback_client(request: Request) -> bool:
    if request.client is None:
        return False
    host = request.client.host.rsplit("%", 1)[0]
    if host in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _session_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    """Expose daemon session state without leaking internal checkpoints or assignments."""

    config = record.get("config")
    config = config if isinstance(config, Mapping) else {}
    return {
        "schema_version": "1.0",
        "session_id": record.get("session_id", record.get("id")),
        "status": record.get("status"),
        "kind": config.get("kind", "interactive"),
        "title": config.get("title"),
        "assignment_id": record.get("assignment_id"),
        "context_summary": record.get("context_summary") or None,
        "summary_through_event_seq": int(
            record.get("summary_through_event_seq", 0)
        ),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
        "usage": {
            "token_count": int(record.get("token_count", 0)),
            "cost_usd": float(record.get("cost_usd", 0.0)),
            "tool_count": int(record.get("tool_count", 0)),
            "model_call_count": int(record.get("model_call_count", 0)),
        },
    }


def _operation_projection(record: Mapping[str, Any]) -> dict[str, Any]:
    session_id = record.get("session_id", record.get("id"))
    cursor = record.get("event_cursor", record.get("seq", 0))
    return {
        "session_id": session_id,
        "status": record.get("status", "accepted"),
        "event_cursor": int(cursor or 0),
        "accepted_at": record.get("accepted_at", datetime.now(timezone.utc).isoformat()),
    }


def _credential_projection(
    record: Mapping[str, Any],
    *,
    body: CredentialProvisionRequest | None = None,
) -> dict[str, Any]:
    """Return credential metadata only; this function never accepts a secret field."""

    result: dict[str, Any] = {
        "credential_ref": record.get("credential_ref"),
        "assignment_id": record.get("assignment_id"),
        "expires_at": record.get("expires_at"),
        "provisioned_at": record.get("created_at"),
        "revoked_at": record.get("revoked_at"),
    }
    if body is not None:
        result["kind"] = body.kind.value
        result["scopes"] = [scope.value for scope in body.scopes]
    elif record.get("name") is not None:
        result["kind"] = record["name"]
    return result


async def _durable_sse_stream(
    *,
    storage: LiliesStorage,
    session_id: str,
    client_id: str,
    after: int,
    request: Request,
    poll_seconds: float,
    heartbeat_seconds: float = 15.0,
) -> AsyncIterator[str]:
    """Replay and follow only committed database events.

    There is deliberately no process-local event queue here. A daemon restart can
    therefore continue from the persisted reader acknowledgement or Last-Event-ID.
    """

    cursor = max(0, after)
    loop = asyncio.get_running_loop()
    heartbeat_at = loop.time() + heartbeat_seconds
    while not await request.is_disconnected():
        events = await storage.list_events(
            session_id,
            after=cursor,
            limit=1000,
            client_id=client_id,
        )
        if events:
            for event in events:
                cursor = int(event["seq"])
                event_type = str(event["event_type"])
                payload = json.dumps(
                    event["data"],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield f"id: {cursor}\nevent: {event_type}\ndata: {payload}\n\n"
            heartbeat_at = loop.time() + heartbeat_seconds
            continue
        now = loop.time()
        if now >= heartbeat_at:
            yield ": keep-alive\n\n"
            heartbeat_at = now + heartbeat_seconds
        await asyncio.sleep(min(max(0.01, poll_seconds), max(0.01, heartbeat_at - now)))


def _event_cursor(request: Request, query_after: int, persisted_ack: int) -> int:
    del query_after
    header = request.headers.get("last-event-id")
    if header is not None:
        if not header.isdigit():
            raise HTTPException(status_code=422, detail="Last-Event-ID must be a non-negative integer")
    # Last-Event-ID and query parameters are connection-local hints, not proof that
    # the client processed an event. Only the durable acknowledgement may advance
    # the replay cursor; otherwise a forged/future header can skip committed work.
    return max(0, persisted_ack)


def create_lilies_app(
    settings: LiliesSettings,
    provider: ModelProvider | None = None,
) -> FastAPI:
    settings.prepare()
    storage = LiliesStorage(settings.data_dir)
    service = _build_service(settings, storage, provider)
    started_at = datetime.now(timezone.utc)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        initialize = getattr(service, "initialize", None)
        if initialize is not None:
            app.state.recovery = await initialize()
        else:
            app.state.recovery = await storage.initialize()
        try:
            yield
        finally:
            shutdown = getattr(service, "shutdown", None)
            if shutdown is not None:
                await shutdown()

    app = FastAPI(
        title="Lilies Local Daemon",
        version=settings.agent_version,
        lifespan=lifespan,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.state.lilies_service = service
    app.state.lilies_storage = storage
    app.state.settings = settings
    app.state.started_at = started_at
    app.state.stopping = False
    app.state.request_daemon_stop = lambda: None

    @app.middleware("http")
    async def reject_non_loopback(request: Request, call_next: Callable[[Request], Any]) -> Any:
        if not _is_loopback_client(request):
            return JSONResponse(status_code=403, content={"detail": "loopback access required"})
        return await call_next(request)

    @app.exception_handler(LiliesAuthenticationError)
    async def authentication_error(_: Request, __: LiliesAuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "invalid local client credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(LiliesAccessDeniedError)
    async def access_error(_: Request, __: LiliesAccessDeniedError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": "forbidden"})

    @app.exception_handler(LiliesNotFoundError)
    async def not_found_error(_: Request, __: LiliesNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "resource not found"})

    @app.exception_handler(LiliesConflictError)
    async def conflict_error(_: Request, __: LiliesConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": "request conflicts with current resource state"},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        # Pydantic includes the rejected input by default. Never reflect a malformed
        # credential secret or another user payload back over the daemon boundary.
        details = [
            {key: value for key, value in item.items() if key not in {"ctx", "input", "url"}}
            for item in error.errors()
        ]
        serialized = json.dumps(details, ensure_ascii=False, default=str).casefold()
        if "collaboration" in serialized:
            details = [
                {
                    "type": "validation_error",
                    "loc": ["body"],
                    "msg": "request validation failed",
                    "input": None,
                }
            ]
        return JSONResponse(status_code=422, content={"detail": details})

    bearer = HTTPBearer(auto_error=False)

    def require_scope(scope: LocalScope) -> Callable[..., Awaitable[ClientRecord]]:
        async def dependency(
            credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        ) -> ClientRecord:
            if credentials is None or credentials.scheme.casefold() != "bearer":
                raise LiliesAuthenticationError("missing bearer token")
            client = await storage.authenticate_client(
                credentials.credentials,
                required_scope=scope.value,
            )
            current_fingerprint = settings.daemon_fingerprint()
            paired_fingerprint = str(client.get("daemon_fingerprint", ""))
            if not hmac.compare_digest(paired_fingerprint, current_fingerprint):
                await storage.append_security_event(
                    "client.authentication_rejected",
                    {"reason": "daemon_fingerprint_mismatch"},
                    client_id=str(client["client_id"]),
                )
                raise LiliesAuthenticationError("client token belongs to another daemon")
            return client

        return dependency

    session_reader = require_scope(LocalScope.session_read)
    session_writer = require_scope(LocalScope.session_write)
    permission_resolver = require_scope(LocalScope.permission_resolve)
    daemon_controller = require_scope(LocalScope.daemon_control)
    credential_writer = require_scope(LocalScope.credential_write)

    @app.get("/local/v1/health", response_model=DaemonHealth)
    async def health() -> dict[str, Any]:
        # Health intentionally contains neither client, credential nor session data.
        return {
            "schema_version": settings.schema_version,
            "service": "lilies",
            "status": "ok",
            "daemon_version": settings.agent_version,
        }

    @app.post("/local/v1/pairings/code", response_model=PairingCodeResult)
    async def create_pairing_code(body: PairingCodeCreateRequest) -> dict[str, Any]:
        scopes = [scope.value for scope in body.allowed_scopes]
        result = await storage.create_pairing_code(
            ttl_seconds=body.ttl_seconds,
            allowed_scopes=scopes,
        )
        return {
            "pairing_code": result["pairing_code"],
            "allowed_scopes": scopes,
            "expires_at": result["expires_at"],
            "daemon_fingerprint": settings.daemon_fingerprint(),
        }

    @app.post("/local/v1/pairings/exchange", response_model=PairingExchangeResult)
    async def exchange_pairing_code(body: PairingExchangeRequest) -> dict[str, Any]:
        requested = [scope.value for scope in body.requested_scopes]
        ttl = (
            settings.cli_token_ttl_seconds
            if body.client_name.startswith("cli:")
            else settings.platform_token_ttl_seconds
        )
        result = await storage.exchange_pairing_code(
            body.pairing_code,
            body.client_name,
            requested,
            body.client_nonce,
            settings.daemon_fingerprint(),
            token_ttl_seconds=ttl,
            previous_client_id=(
                str(body.previous_client_id) if body.previous_client_id is not None else None
            ),
            previous_access_token=(
                body.previous_access_token.get_secret_value()
                if body.previous_access_token is not None
                else None
            ),
            requested_client_id=(
                str(body.requested_client_id)
                if body.requested_client_id is not None
                else None
            ),
            prepared_access_token=(
                body.prepared_access_token.get_secret_value()
                if body.prepared_access_token is not None
                else None
            ),
        )
        return result

    @app.get("/local/v1/status", response_model=DaemonStatus)
    async def daemon_status(
        client: ClientRecord = Depends(session_reader),
    ) -> dict[str, Any]:
        service_status = await service.status()
        return {
            "schema_version": settings.schema_version,
            "pid": os.getpid(),
            "address": settings.base_url,
            "started_at": started_at.isoformat(),
            "daemon_fingerprint": settings.daemon_fingerprint(),
            "client_id": client["client_id"],
            "client_scopes": client["scopes"],
            "client_expires_at": client.get("expires_at"),
            "provider": service_status["provider"],
            "model": service_status["model"],
            "paired_client_count": service_status["paired_client_count"],
            "platform_paired": service_status["platform_paired"],
            "active_session_count": service_status["active_session_count"],
            "active_assignment_count": service_status["active_assignment_count"],
            "stopping": bool(app.state.stopping or service_status["stopping"]),
        }

    @app.get("/local/v1/sessions", response_model=SessionListResult)
    async def list_sessions(
        client: ClientRecord = Depends(session_reader),
    ) -> dict[str, Any]:
        records = await storage.list_sessions(client_id=client["client_id"])
        return {"sessions": [_session_projection(record) for record in records]}

    @app.post("/local/v1/sessions", status_code=201, response_model=SessionResult)
    async def create_session(
        body: SessionCreateRequest,
        client: ClientRecord = Depends(session_writer),
    ) -> dict[str, Any]:
        result = await service.create_session(body, client_id=client["client_id"])
        return _session_projection(result)

    @app.get("/local/v1/sessions/{session_id}", response_model=SessionResult)
    async def get_session(
        session_id: str,
        client: ClientRecord = Depends(session_reader),
    ) -> dict[str, Any]:
        result = await storage.get_session(session_id, client_id=client["client_id"])
        return _session_projection(result)

    @app.post(
        "/local/v1/sessions/{session_id}/messages",
        status_code=202,
        response_model=SessionOperationResult,
    )
    async def send_message(
        session_id: str,
        body: SessionMessageRequest,
        client: ClientRecord = Depends(session_writer),
    ) -> dict[str, Any]:
        await storage.get_session(session_id, client_id=client["client_id"])
        result = await service.submit_message(session_id, body, client_id=client["client_id"])
        return _operation_projection(result)

    @app.post(
        "/local/v1/sessions/{session_id}/formal-workspace",
        status_code=201,
        response_model=FormalWorkspaceStagingReceipt,
    )
    async def stage_formal_workspace(
        session_id: str,
        body: FormalWorkspaceStagingRequest,
        client: ClientRecord = Depends(session_writer),
    ) -> dict[str, Any]:
        return await service.stage_formal_workspace(
            session_id,
            body,
            client_id=client["client_id"],
        )

    @app.post(
        "/local/v1/sessions/{session_id}/assignments",
        status_code=202,
        response_model=AssignmentSubmissionResult,
    )
    async def submit_assignment(
        session_id: str,
        body: BuildAssignment,
        client: ClientRecord = Depends(session_writer),
    ) -> dict[str, Any]:
        return await service.submit_assignment(
            session_id,
            body,
            client_id=client["client_id"],
        )

    @app.post(
        "/local/v1/sessions/{session_id}/resume",
        status_code=202,
        response_model=SessionOperationResult,
    )
    async def resume_session(
        session_id: str,
        body: SessionResumeRequest,
        client: ClientRecord = Depends(session_writer),
    ) -> dict[str, Any]:
        await storage.get_session(session_id, client_id=client["client_id"])
        result = await service.resume_session(session_id, body, client_id=client["client_id"])
        return _operation_projection(result)

    @app.post(
        "/local/v1/sessions/{session_id}/cancel",
        status_code=202,
        response_model=SessionOperationResult,
    )
    async def cancel_session(
        session_id: str,
        body: SessionCancelRequest,
        client: ClientRecord = Depends(session_writer),
    ) -> dict[str, Any]:
        await storage.get_session(session_id, client_id=client["client_id"])
        result = await service.cancel_session(session_id, body, client_id=client["client_id"])
        return _operation_projection(result)

    @app.post(
        "/local/v1/sessions/{session_id}/permissions/{request_id}",
        response_model=PermissionDecisionResult,
    )
    async def resolve_permission(
        session_id: str,
        request_id: str,
        body: PermissionDecisionRequest,
        client: ClientRecord = Depends(permission_resolver),
    ) -> dict[str, Any]:
        await storage.get_session(session_id, client_id=client["client_id"])
        result = await service.resolve_permission(
            session_id,
            request_id,
            body,
            client_id=client["client_id"],
        )
        return {
            "request_id": result.get("request_id", result.get("id")),
            "status": result["status"],
            "input_digest": result["input_digest"],
            "decided_at": result.get("resolved_at"),
        }

    @app.get("/local/v1/sessions/{session_id}/events")
    async def session_events(
        session_id: str,
        request: Request,
        client: ClientRecord = Depends(session_reader),
        after: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        client_id = client["client_id"]
        await storage.get_session(session_id, client_id=client_id)
        ack = await storage.get_ack(client_id, session_id)
        cursor = _event_cursor(request, after, int(ack["cursor"]))
        return StreamingResponse(
            _durable_sse_stream(
                storage=storage,
                session_id=session_id,
                client_id=client_id,
                after=cursor,
                request=request,
                poll_seconds=settings.event_poll_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/local/v1/sessions/{session_id}/acks",
        response_model=SessionAckResult,
    )
    async def acknowledge_events(
        session_id: str,
        body: SessionAckRequest,
        client: ClientRecord = Depends(session_reader),
    ) -> dict[str, Any]:
        return await storage.ack_events(client["client_id"], session_id, body.cursor)

    @app.post(
        "/local/v1/credentials/provision",
        status_code=201,
        response_model=CredentialProvisionResult,
    )
    async def provision_credential(
        body: CredentialProvisionRequest,
        client: ClientRecord = Depends(credential_writer),
    ) -> dict[str, Any]:
        result = await storage.provision_credential(
            body.kind.value,
            body.secret.get_secret_value(),
            scopes=[scope.value for scope in body.scopes],
            idempotency_key=body.idempotency_key,
            credential_ref=body.credential_ref,
            client_id=client["client_id"],
            assignment_id=str(body.assignment_id),
            expires_at=body.expires_at,
        )
        return _credential_projection(result, body=body)

    @app.post(
        "/local/v1/credentials/revoke",
        response_model=CredentialRevokeResult,
    )
    async def revoke_credential(
        body: CredentialRevokeRequest,
        client: ClientRecord = Depends(credential_writer),
    ) -> dict[str, Any]:
        candidates = await storage.list_credentials()
        record = next(
            (
                item
                for item in candidates
                if item.get("credential_ref") == body.credential_ref
            ),
            None,
        )
        if record is None:
            raise LiliesNotFoundError("credential not found")
        if record.get("client_id") != client["client_id"]:
            raise LiliesAccessDeniedError("credential belongs to another client")
        result = await storage.revoke_credential(
            body.credential_ref,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
        )
        return {
            "credential_ref": result["credential_ref"],
            "revoked": result.get("revoked_at") is not None,
            "revoked_at": result.get("revoked_at"),
        }

    @app.post(
        "/local/v1/control/stop",
        status_code=202,
        response_model=DaemonStopResult,
    )
    async def stop_daemon(
        body: DaemonStopRequest,
        _: ClientRecord = Depends(daemon_controller),
    ) -> dict[str, Any]:
        active_turns = int(await service.request_stop(reason=body.reason))
        app.state.stopping = True
        callback: StopCallback = app.state.request_daemon_stop
        callback_result = callback()
        if inspect.isawaitable(callback_result):
            await callback_result
        return {
            "accepted": True,
            "active_turns_cancel_requested": active_turns,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }

    return app
