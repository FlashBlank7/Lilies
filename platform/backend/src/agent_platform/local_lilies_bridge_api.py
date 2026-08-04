from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from uuid import UUID

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .local_lilies_bridge import (
    LocalLiliesBridge,
    LocalLiliesBridgeError,
    LocalLiliesMessagePage,
    LocalLiliesObservabilitySnapshot,
    LocalLiliesRelayEvent,
    LocalLiliesUsagePage,
    PairLocalLiliesRequest,
    ReconnectLocalLiliesRequest,
    StartFormalLocalLiliesBuildRequest,
    StartLocalLiliesBuildRequest,
)
from .lilies_models import (
    PermissionDecisionRequest,
    SessionMessageRequest,
    SessionOperationResult,
)
from .platform_blackbox_auth import PlatformBlackboxScope


_SSE_EVENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_OBSERVABILITY_SNAPSHOT_PATH = re.compile(
    r"^/api/v1/local-lilies/connections/[^/]+/observability-snapshot$"
)
_MESSAGE_HISTORY_PATH = re.compile(
    r"^/api/v1/local-lilies/assignments/[^/]+/messages$"
)
_MAX_EVENT_CURSOR = 2**63 - 1
_Result = TypeVar("_Result")
AuthDependency = Callable[..., Awaitable[None]]
FormalVerificationProvider = Callable[[UUID], Awaitable[Any]]


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LocalLiliesCancelRequest(_StrictRequest):
    idempotency_key: str | None = Field(default=None, min_length=16, max_length=128)
    reason: str = Field(default="requested_by_user", min_length=1, max_length=1_000)


class LocalLiliesRelayRequest(_StrictRequest):
    max_events: int = Field(default=100, ge=1, le=1_000)


@dataclass(frozen=True, slots=True)
class _ContractCredentialView:
    """Least-authority fields used to render the published contract."""

    scopes: tuple[PlatformBlackboxScope, ...]
    application_ids: tuple[UUID, ...]
    allowed_operations: tuple[Any, ...]
    connector_access: bool
    allowed_network_hosts: tuple[str, ...]
    readable_host_objects: tuple[str, ...]
    writable_host_operations: tuple[str, ...]
    permission_required_actions: tuple[str, ...]
    compensation_actions: tuple[str, ...]
    max_payload_bytes: int
    allowed_actions_digest: str | None
    budget_digest: str | None


async def published_platform_contract_digest(
    services: Any,
    scopes: tuple[PlatformBlackboxScope, ...],
    application_ids: tuple[UUID, ...],
    allowed_actions: Any,
) -> str:
    """Render through the exact facade used by ``platform-contract``.

    Keeping this callback on the facade matters: published workflow tools and
    the monotonic contract-version store must influence the assignment digest
    exactly as they do for the daemon's subsequent HTTP contract read.
    """

    # Imported lazily so the thin bridge route module does not take ownership
    # of the public black-box contract or introduce an api.py import cycle.
    from .lilies_platform_api import _current_contract
    from .platform_blackbox_auth import PlatformBlackboxOperation

    # Formal assignments carry a frozen host-action policy object, while the
    # ordinary customer Builder route carries only its public platform action
    # tuple.  Both routes use this callback, so project the latter as an
    # intentionally connector-free credential instead of assuming the formal
    # shape.
    if hasattr(allowed_actions, "platform_actions"):
        platform_actions = allowed_actions.platform_actions
        connector_access = allowed_actions.connector_access
        allowed_network_hosts = tuple(allowed_actions.network_hosts)
        readable_host_objects = tuple(allowed_actions.readable_host_objects)
        writable_host_operations = tuple(
            allowed_actions.writable_host_operations
        )
        permission_required_actions = tuple(
            allowed_actions.permission_required_actions
        )
        compensation_actions = tuple(allowed_actions.compensation_actions)
        max_payload_bytes = allowed_actions.max_payload_bytes
        allowed_actions_digest = f"sha256:{'0' * 64}"
        budget_digest = f"sha256:{'0' * 64}"
    else:
        # Customer credentials are issued with the public operation enum's
        # defaults and are narrowed by scopes plus application binding.  The
        # digest must mirror that issued credential exactly; the assignment's
        # allowed-actions tuple separately narrows the daemon tool registry.
        platform_actions = tuple(PlatformBlackboxOperation)
        connector_access = False
        allowed_network_hosts = ()
        readable_host_objects = ()
        writable_host_operations = ()
        permission_required_actions = ()
        compensation_actions = ()
        max_payload_bytes = 100 * 1024 * 1024
        allowed_actions_digest = None
        budget_digest = None

    contract = await _current_contract(
        services,
        _ContractCredentialView(
            scopes=scopes,
            application_ids=application_ids,
            allowed_operations=tuple(
                PlatformBlackboxOperation(action.value)
                for action in platform_actions
            ),
            connector_access=connector_access,
            allowed_network_hosts=allowed_network_hosts,
            readable_host_objects=readable_host_objects,
            writable_host_operations=writable_host_operations,
            permission_required_actions=permission_required_actions,
            compensation_actions=compensation_actions,
            max_payload_bytes=max_payload_bytes,
            # The published contract needs only the governed/non-governed
            # distinction.  Exact package digests are bound into the issued
            # credential and handoff after broker preparation.
            allowed_actions_digest=allowed_actions_digest,
            budget_digest=budget_digest,
        ),
    )
    return str(contract["contract_digest"])


def _http_error(error: LocalLiliesBridgeError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.public_detail())


async def _bridge_call(result: Awaitable[_Result]) -> _Result:
    try:
        return await result
    except LocalLiliesBridgeError as error:
        raise _http_error(error) from error


def _resume_cursor(after: int, last_event_id: str | None) -> int:
    if last_event_id is None or not last_event_id.strip():
        return after
    value = last_event_id.strip()
    if not value.isascii() or not value.isdecimal():
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_event_cursor",
                "message": "Last-Event-ID must be a non-negative integer cursor",
            },
        )
    parsed = int(value)
    if parsed > _MAX_EVENT_CURSOR:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_event_cursor",
                "message": "Last-Event-ID exceeds the supported cursor range",
            },
        )
    return max(after, parsed)


def _event_payload(
    event: LocalLiliesRelayEvent,
    *,
    replay_boundary: int,
) -> dict[str, Any]:
    return {
        "event_id": f"{event.assignment_id}:{event.daemon_seq}",
        "assignment_id": str(event.assignment_id),
        "session_id": str(event.session_id),
        "seq": event.daemon_seq,
        "daemon_seq": event.daemon_seq,
        "event_type": event.event_type,
        "data": event.data,
        "replayed": event.daemon_seq <= replay_boundary,
        "created_at": event.received_at.isoformat(),
    }


def _encode_sse(
    event: LocalLiliesRelayEvent,
    *,
    replay_boundary: int,
) -> str:
    payload = json.dumps(
        _event_payload(event, replay_boundary=replay_boundary),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    event_line = (
        f"event: {event.event_type}\n" if _SSE_EVENT_NAME.fullmatch(event.event_type) else ""
    )
    return f"id: {event.daemon_seq}\n{event_line}data: {payload}\n\n"


def _encode_stream_error(error: LocalLiliesBridgeError) -> str:
    payload = json.dumps(
        error.public_detail(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Deliberately omit an id: the browser must retain the last persisted
    # daemon cursor when reconnecting after this transient bridge failure.
    return f"event: bridge.error\nretry: 1000\ndata: {payload}\n\n"


def _safe_validation_errors(error: RequestValidationError) -> list[dict[str, Any]]:
    messages = {
        "missing": "Field required",
        "extra_forbidden": "Extra inputs are not permitted",
    }
    safe: list[dict[str, Any]] = []
    for item in error.errors():
        error_type = str(item.get("type") or "value_error")[:120]
        location = [
            value if isinstance(value, int) else str(value)[:160] for value in item.get("loc", ())
        ]
        safe.append(
            {
                "loc": location,
                "msg": messages.get(error_type, "Request value is invalid"),
                "type": error_type,
            }
        )
    return safe


async def _assignment_event_stream(
    bridge: LocalLiliesBridge,
    request: Request,
    assignment_id: UUID,
    *,
    cursor: int,
    replay_boundary: int,
    initial_events: list[LocalLiliesRelayEvent],
    poll_seconds: float = 0.25,
) -> AsyncIterator[str]:
    pending = initial_events
    last_keepalive = asyncio.get_running_loop().time()
    while True:
        if await request.is_disconnected():
            return
        for event in pending:
            if event.daemon_seq <= cursor:
                continue
            yield _encode_sse(event, replay_boundary=replay_boundary)
            cursor = event.daemon_seq
            if await request.is_disconnected():
                return
        try:
            await bridge.relay_events(assignment_id, max_events=100)
            pending = await bridge.list_events(assignment_id, after=cursor)
        except LocalLiliesBridgeError as error:
            yield _encode_stream_error(error)
            return
        if pending:
            continue
        now = asyncio.get_running_loop().time()
        if now - last_keepalive >= 15:
            yield ": keep-alive\n\n"
            last_keepalive = now
        await asyncio.sleep(max(0.01, poll_seconds))


def install_local_lilies_bridge_api(
    app: FastAPI,
    bridge: LocalLiliesBridge,
    *,
    require_token: AuthDependency,
    formal_verification_provider: FormalVerificationProvider | None = None,
) -> None:
    """Install thin, platform-token-only routes over ``LocalLiliesBridge``."""

    previous_validation_handler = app.exception_handlers.get(RequestValidationError)

    @app.middleware("http")
    async def local_lilies_sensitive_projection_no_store(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        normalized_path = request.url.path.rstrip("/")
        is_sensitive_projection = any(
            pattern.fullmatch(normalized_path) is not None
            for pattern in (_OBSERVABILITY_SNAPSHOT_PATH, _MESSAGE_HISTORY_PATH)
        )
        try:
            response = await call_next(request)
        except Exception:
            if not is_sensitive_projection:
                raise
            is_observability = (
                _OBSERVABILITY_SNAPSHOT_PATH.fullmatch(normalized_path) is not None
            )
            response = JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "code": (
                            "local_lilies_observability_internal_error"
                            if is_observability
                            else "local_lilies_transcript_internal_error"
                        ),
                        "message": (
                            "local Lilies observability request failed"
                            if is_observability
                            else "local Lilies transcript request failed"
                        ),
                    }
                },
            )
        if is_sensitive_projection:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def local_lilies_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> Any:
        if not request.url.path.startswith("/api/v1/local-lilies/"):
            if previous_validation_handler is not None:
                return await previous_validation_handler(request, error)
            return await request_validation_exception_handler(request, error)
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_local_lilies_request",
                    "message": "request did not match the local Lilies bridge schema",
                    "errors": _safe_validation_errors(error),
                }
            },
        )

    async def require_enabled() -> None:
        try:
            bridge.require_enabled()
        except LocalLiliesBridgeError as error:
            raise _http_error(error) from error

    dependencies = [Depends(require_token), Depends(require_enabled)]

    @app.get(
        "/api/v1/local-lilies/status",
        dependencies=[Depends(require_token)],
    )
    async def local_lilies_status() -> dict[str, Any]:
        if bridge.enabled:
            return await _bridge_call(bridge.status())
        return await _bridge_call(bridge.discovery_status())

    @app.post("/api/v1/local-lilies/connections", dependencies=dependencies)
    async def pair_local_lilies(body: PairLocalLiliesRequest) -> dict[str, Any]:
        await _bridge_call(bridge.pair_connection(body))
        return await _bridge_call(bridge.status())

    @app.get("/api/v1/local-lilies/connections", dependencies=dependencies)
    async def list_local_lilies_connections() -> list[Any]:
        return await _bridge_call(bridge.list_connections())

    @app.get(
        "/api/v1/local-lilies/connections/{connection_id}",
        dependencies=dependencies,
    )
    async def get_local_lilies_connection(connection_id: UUID) -> Any:
        return await _bridge_call(bridge.get_connection(connection_id))

    @app.post(
        "/api/v1/local-lilies/connections/{connection_id}/refresh",
        dependencies=dependencies,
    )
    async def refresh_local_lilies_connection(connection_id: UUID) -> Any:
        return await _bridge_call(bridge.refresh_connection(connection_id))

    @app.get(
        "/api/v1/local-lilies/connections/{connection_id}/usage",
        response_model=LocalLiliesUsagePage,
        dependencies=dependencies,
    )
    async def local_lilies_usage(
        connection_id: UUID,
        group_by: list[str] = Query(default=["session", "stage", "model"]),
        page: int = Query(default=1, ge=1, le=1_000),
        page_size: int = Query(default=100, ge=1, le=100),
    ) -> Any:
        if (
            not group_by
            or len(group_by) > 3
            or len(set(group_by)) != len(group_by)
            or any(value not in {"session", "stage", "model"} for value in group_by)
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_local_lilies_request",
                    "message": "usage group_by must contain unique session, stage, or model values",
                },
            )
        return await _bridge_call(
            bridge.usage(
                connection_id,
                group_by=tuple(group_by),
                page=page,
                page_size=page_size,
            )
        )

    @app.get(
        "/api/v1/local-lilies/connections/{connection_id}/observability-snapshot",
        response_model=LocalLiliesObservabilitySnapshot,
        dependencies=dependencies,
    )
    async def local_lilies_observability_snapshot(
        connection_id: UUID,
    ) -> Any:
        return await _bridge_call(bridge.observability_snapshot(connection_id))

    @app.post(
        "/api/v1/local-lilies/connections/{connection_id}/reconnect",
        dependencies=dependencies,
    )
    async def reconnect_local_lilies_connection(
        connection_id: UUID,
        body: ReconnectLocalLiliesRequest,
    ) -> dict[str, Any]:
        await _bridge_call(bridge.reconnect_connection(connection_id, body))
        return await _bridge_call(bridge.status())

    @app.post(
        "/api/v1/local-lilies/applications/{application_id}/builds",
        dependencies=dependencies,
    )
    async def start_local_lilies_build(
        application_id: UUID,
        body: StartLocalLiliesBuildRequest,
    ) -> Any:
        return await _bridge_call(bridge.start_build(application_id, body))

    @app.post(
        "/api/v1/local-lilies/applications/{application_id}/formal-builds",
        dependencies=dependencies,
    )
    async def start_formal_local_lilies_build(
        application_id: UUID,
        body: StartFormalLocalLiliesBuildRequest,
    ) -> Any:
        return await _bridge_call(bridge.start_formal_build(application_id, body))

    @app.get(
        "/api/v1/local-lilies/applications/{application_id}/assignments",
        dependencies=dependencies,
    )
    async def list_local_lilies_assignments(application_id: UUID) -> list[Any]:
        return await _bridge_call(bridge.list_assignments_for_application(application_id))

    @app.get(
        "/api/v1/local-lilies/assignments/{assignment_id}",
        dependencies=dependencies,
    )
    async def get_local_lilies_assignment(assignment_id: UUID) -> Any:
        return await _bridge_call(bridge.get_assignment(assignment_id))

    @app.post(
        "/api/v1/local-lilies/assignments/{assignment_id}/independent-verification",
        dependencies=dependencies,
    )
    async def independently_verify_local_lilies_assignment(
        assignment_id: UUID,
    ) -> Any:
        assignment = await _bridge_call(bridge.get_assignment(assignment_id))
        if assignment.phase != "completed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "formal_assignment_not_completed",
                    "message": ("independent verification requires a completed formal assignment"),
                },
            )
        if formal_verification_provider is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "formal_verifier_unavailable",
                    "message": "formal independent verification is unavailable",
                },
            )
        try:
            return await formal_verification_provider(assignment_id)
        except Exception as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "formal_verification_rejected",
                    "message": ("the frozen formal claim could not be independently verified"),
                },
            ) from error

    @app.get("/api/v1/local-lilies/builds/{build_id}", dependencies=dependencies)
    async def get_local_lilies_assignment_by_build(build_id: UUID) -> Any:
        return await _bridge_call(bridge.get_assignment_by_build(build_id))

    @app.get("/api/v1/local-lilies/sessions/{session_id}", dependencies=dependencies)
    async def get_local_lilies_assignment_by_session(session_id: UUID) -> Any:
        return await _bridge_call(bridge.get_assignment_by_session(session_id))

    @app.post(
        "/api/v1/local-lilies/assignments/{assignment_id}/resume",
        dependencies=dependencies,
    )
    async def resume_local_lilies_assignment(assignment_id: UUID) -> Any:
        return await _bridge_call(bridge.resume_assignment(assignment_id))

    @app.post(
        "/api/v1/local-lilies/assignments/{assignment_id}/cancel",
        dependencies=dependencies,
    )
    async def cancel_local_lilies_assignment(
        assignment_id: UUID,
        body: LocalLiliesCancelRequest | None = Body(default=None),
    ) -> Any:
        request_body = body or LocalLiliesCancelRequest()
        idempotency_key = request_body.idempotency_key or (f"platform.cancel.{assignment_id.hex}")
        return await _bridge_call(
            bridge.cancel_assignment(
                assignment_id,
                idempotency_key=idempotency_key,
                reason=request_body.reason,
            )
        )

    @app.post(
        "/api/v1/local-lilies/assignments/{assignment_id}/permissions/{request_id}",
        dependencies=dependencies,
    )
    async def resolve_local_lilies_assignment_permission(
        assignment_id: UUID,
        request_id: UUID,
        body: PermissionDecisionRequest,
    ) -> Any:
        return await _bridge_call(
            bridge.resolve_assignment_permission(
                assignment_id,
                request_id,
                body,
            )
        )

    @app.get(
        "/api/v1/local-lilies/assignments/{assignment_id}/messages",
        response_model=LocalLiliesMessagePage,
        dependencies=dependencies,
    )
    async def list_local_lilies_assignment_messages(
        assignment_id: UUID,
        limit: int = Query(default=20, ge=1, le=20),
        before: UUID | None = Query(default=None),
    ) -> Any:
        return await _bridge_call(
            bridge.list_assignment_messages(
                assignment_id,
                limit=limit,
                before=before,
            )
        )

    @app.post(
        "/api/v1/local-lilies/assignments/{assignment_id}/messages",
        response_model=SessionOperationResult,
        dependencies=dependencies,
    )
    async def send_local_lilies_assignment_message(
        assignment_id: UUID,
        body: SessionMessageRequest,
    ) -> Any:
        return await _bridge_call(
            bridge.send_assignment_message(assignment_id, body)
        )

    @app.post(
        "/api/v1/local-lilies/assignments/{assignment_id}/relay",
        dependencies=dependencies,
    )
    async def relay_local_lilies_events(
        assignment_id: UUID,
        body: LocalLiliesRelayRequest | None = Body(default=None),
    ) -> Any:
        max_events = body.max_events if body is not None else 100
        return await _bridge_call(bridge.relay_events(assignment_id, max_events=max_events))

    @app.get(
        "/api/v1/local-lilies/assignments/{assignment_id}/events",
        dependencies=dependencies,
    )
    async def stream_local_lilies_events(
        assignment_id: UUID,
        request: Request,
        after: int = Query(default=0, ge=0, le=_MAX_EVENT_CURSOR),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        cursor = _resume_cursor(after, last_event_id)
        assignment = await _bridge_call(bridge.get_assignment(assignment_id))
        replay_boundary = assignment.relay_cursor
        initial_events = await _bridge_call(bridge.list_events(assignment_id, after=cursor))
        if not initial_events:
            # Keep startup failures as structured HTTP 503 responses.  If a
            # persisted replay exists, deliver it first and surface any later
            # daemon loss as a cursor-preserving SSE bridge.error event.
            await _bridge_call(bridge.relay_events(assignment_id, max_events=100))
            initial_events = await _bridge_call(bridge.list_events(assignment_id, after=cursor))
        return StreamingResponse(
            _assignment_event_stream(
                bridge,
                request,
                assignment_id,
                cursor=cursor,
                replay_boundary=replay_boundary,
                initial_events=initial_events,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Last-Event-ID": str(cursor),
                "X-Accel-Buffering": "no",
            },
        )
