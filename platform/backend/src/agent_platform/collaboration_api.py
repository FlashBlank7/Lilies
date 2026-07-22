from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from starlette.routing import Match

from .collaboration_models import (
    ApprovalDecisionRequest as CollaborationReportDecisionRequest,
    ChannelCloseRequest as CollaborationChannelCloseRequest,
    ChannelStatus,
    ChannelSettingsRequest as CollaborationChannelSettingsRequest,
    DeveloperResponseRequest as CollaborationDeveloperResponseRequest,
    EnvironmentResponseRequest as CollaborationEnvironmentResponseRequest,
    LeaseAcquireRequest as CollaborationLeaseAcquireRequest,
    LeaseReleaseRequest as CollaborationLeaseReleaseRequest,
    LeaseRenewRequest as CollaborationLeaseRenewRequest,
    LiliesReprobeResultRequest as CollaborationReprobeRequest,
    ReaderAckRequest as CollaborationAckRequest,
    ReportRevisionRequest as CollaborationReportRevisionRequest,
    ReportRoute,
    ReportSubmitRequest as CollaborationReportSubmitRequest,
    ReportWithdrawalRequest as CollaborationReportWithdrawalRequest,
    TaskPackageAmendmentRequest as CollaborationTaskAmendmentRequest,
    VerificationClaimRequest as CollaborationVerificationClaimRequest,
    VerificationResultRequest as CollaborationVerificationResultRequest,
)


_MAX_EVENT_CURSOR = 2**63 - 1
_SSE_EVENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_FORBIDDEN_QUERY_KEYS = frozenset(
    {
        "token",
        "api_token",
        "access_token",
        "frontend_token",
        "developer_token",
        "verifier_token",
        "collaboration_token",
        "api_key",
        "authorization",
        "cookie",
        "password",
        "secret",
    }
)
_COLLABORATION_PREFIXES = (
    "/api/v1/collaboration/",
    "/api/v1/studio/collaboration/",
    "/api/v1/developer/collaboration/",
)
_Result = TypeVar("_Result")
UserAuthDependency = Callable[..., Awaitable[None]]


@runtime_checkable
class CollaborationServiceContract(Protocol):
    """The narrow service surface consumed by the FastAPI adapter.

    Domain validation, authorization decisions, compare-and-set transitions,
    redaction, and persistence all remain service responsibilities.  The API
    adapter supplies an authenticated principal and never accepts a role from a
    request body.
    """

    def require_enabled(self) -> Any: ...

    def authenticate_lilies(
        self,
        access_token: str,
        *,
        channel_id: UUID,
        required_scope: str,
    ) -> Any: ...

    def authenticate_developer(
        self,
        access_token: str,
        *,
        required_scope: str,
    ) -> Any: ...

    def authenticate_verifier(
        self,
        access_token: str,
        *,
        claim_id: UUID,
        required_scope: str,
    ) -> Any: ...


def _is_collaboration_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _COLLABORATION_PREFIXES)


def _safe_validation_errors(error: RequestValidationError) -> list[dict[str, Any]]:
    messages = {
        "missing": "Field required",
        "extra_forbidden": "Extra inputs are not permitted",
    }
    safe: list[dict[str, Any]] = []
    for item in error.errors():
        error_type = str(item.get("type") or "value_error")[:120]
        location = [
            value if isinstance(value, int) else str(value)[:160]
            for value in item.get("loc", ())
        ]
        safe.append(
            {
                "loc": location,
                "msg": messages.get(error_type, "Request value is invalid"),
                "type": error_type,
            }
        )
    return safe


def _public_error(error: Exception) -> tuple[int, dict[str, Any]] | None:
    status_code = getattr(error, "status_code", None)
    projection = getattr(error, "public_detail", None)
    if not isinstance(status_code, int) or not 400 <= status_code <= 599:
        return None
    if callable(projection):
        detail = projection()
        if not isinstance(detail, Mapping):
            return None
    else:
        code = str(getattr(error, "code", "collaboration_error"))[:120]
        messages = {
            "collaboration_not_found": "collaboration resource was not found",
            "collaboration_conflict": "collaboration request conflicts with persisted state",
            "collaboration_channel_closed": "collaboration channel is closed",
            "collaboration_unauthorized": "collaboration request is not authorized",
            "collaboration_storage_error": "collaboration storage operation failed",
        }
        detail = {
            "code": code,
            "message": messages.get(code, "collaboration request failed"),
        }
    code = str(detail.get("code") or "collaboration_error")[:120]
    message = str(detail.get("message") or "collaboration request failed")[:1_000]
    safe_detail: dict[str, Any] = {"code": code, "message": message}
    identifiers = detail.get("identifiers")
    if isinstance(identifiers, Mapping):
        safe_detail["identifiers"] = {
            str(key)[:120]: str(value)[:200]
            for key, value in identifiers.items()
            if value is not None
            and (
                str(key).casefold().endswith("_id")
                or str(key).casefold() in {"revision", "status", "route", "cursor"}
            )
        }
        if not safe_detail["identifiers"]:
            safe_detail.pop("identifiers")
    return status_code, safe_detail


async def _service_call(result: Awaitable[_Result] | _Result) -> _Result:
    try:
        if inspect.isawaitable(result):
            return await result
        return result
    except HTTPException:
        raise
    except Exception as error:
        public = _public_error(error)
        if public is None:
            raise
        status_code, detail = public
        raise HTTPException(status_code=status_code, detail=detail) from error


async def _service_invoke(
    operation: Callable[..., Awaitable[_Result] | _Result],
    /,
    *args: Any,
    **kwargs: Any,
) -> _Result:
    """Invoke inside the error boundary, including synchronous auth methods."""

    try:
        result = operation(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as error:
        public = _public_error(error)
        if public is None:
            raise
        status_code, detail = public
        raise HTTPException(status_code=status_code, detail=detail) from error
    return await _service_call(result)


def _model_dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _model_dump(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_model_dump(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _model_dump(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _event_sequence(event: Any) -> int:
    projected = _model_dump(event)
    if not isinstance(projected, Mapping):
        raise RuntimeError("collaboration service returned a non-object event")
    value = projected.get("seq")
    if not isinstance(value, int) or value < 1 or value > _MAX_EVENT_CURSOR:
        raise RuntimeError("collaboration service returned an invalid event sequence")
    return value


def _encode_sse(event: Any) -> str:
    projected = _model_dump(event)
    if not isinstance(projected, Mapping):
        raise RuntimeError("collaboration service returned a non-object event")
    seq = _event_sequence(projected)
    event_name = str(projected.get("message_type") or "message")
    event_line = f"event: {event_name}\n" if _SSE_EVENT_NAME.fullmatch(event_name) else ""
    payload = json.dumps(
        projected,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"id: {seq}\n{event_line}data: {payload}\n\n"


def _encode_stream_error(error: Exception) -> str:
    if isinstance(error, HTTPException) and isinstance(error.detail, Mapping):
        detail = {
            "code": str(error.detail.get("code") or "collaboration_stream_failed")[:120],
            "message": str(
                error.detail.get("message") or "collaboration event stream failed"
            )[:1_000],
        }
    else:
        public = _public_error(error)
        detail = (
            public[1]
            if public is not None
            else {
                "code": "collaboration_stream_failed",
                "message": "collaboration event stream failed",
            }
        )
    payload = json.dumps(detail, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    # No id: the reader must retain its last durable acknowledgement.
    return f"event: collaboration.error\nretry: 1000\ndata: {payload}\n\n"


def _requested_cursor(after: int, last_event_id: str | None) -> int:
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


def _hidden_collaboration_error() -> HTTPException:
    # Deliberately identical for a missing resource, missing/wrong bearer,
    # forbidden query credential, cross-channel access, and wrong role.
    return HTTPException(
        status_code=404,
        detail="Not Found",
    )


def _plain_not_found() -> JSONResponse:
    """Match FastAPI's unknown-route projection without redirect or method hints."""

    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def _hidden_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (TypeError, ValueError) as error:
        raise _hidden_collaboration_error() from error


async def _durable_event_stream(
    *,
    service: Any,
    request: Request,
    channel_id: UUID,
    principal: Any,
    cursor: int,
    initial_events: Sequence[Any] = (),
    poll_seconds: float = 0.25,
) -> AsyncIterator[str]:
    last_keepalive = asyncio.get_running_loop().time()
    pending = list(initial_events)
    subscribe = getattr(service, "subscribe_events", None)
    unsubscribe = getattr(service, "unsubscribe_events", None)
    wait_for_event = getattr(service, "wait_for_event", None)
    subscription = subscribe(channel_id) if callable(subscribe) else None
    try:
        while True:
            if await request.is_disconnected():
                return
            try:
                if pending:
                    events = pending
                    pending = []
                else:
                    events = await _service_call(
                        service.list_events(
                            principal=principal,
                            channel_id=channel_id,
                            after=cursor,
                            limit=500,
                        )
                    )
            except Exception as error:
                yield _encode_stream_error(error)
                return
            advanced = False
            for event in events:
                seq = _event_sequence(event)
                if seq <= cursor:
                    continue
                yield _encode_sse(event)
                cursor = seq
                advanced = True
                if await request.is_disconnected():
                    return
            # Visibility filtering can legitimately leave gaps in the global channel
            # sequence.  The service guarantees monotonic ordering for the messages
            # this principal may see; the API must not mistake hidden rows for loss.
            if advanced:
                continue
            now = asyncio.get_running_loop().time()
            if now - last_keepalive >= 15:
                yield ": keep-alive\n\n"
                last_keepalive = now
            try:
                if subscription is not None and callable(wait_for_event):
                    await wait_for_event(subscription, timeout=poll_seconds)
                else:
                    await asyncio.sleep(max(0.01, poll_seconds))
            except Exception as error:
                yield _encode_stream_error(error)
                return
    finally:
        if subscription is not None and callable(unsubscribe):
            unsubscribe(subscription)


def install_collaboration_api(
    app: FastAPI,
    service: CollaborationServiceContract,
    *,
    require_user_token: UserAuthDependency,
) -> None:
    """Install role-separated collaboration routes over a durable service.

    The installer is deliberately independent of ``api.Services`` so the main
    composition root can import this module without an import cycle.
    """

    previous_validation_handler = app.exception_handlers.get(RequestValidationError)

    @app.middleware("http")
    async def collaboration_undiscoverability(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        """Hide the route topology until a caller presents a bearer.

        Dependency-only hiding is too late for Starlette's automatic slash
        redirect and method negotiation.  Those router responses reveal that
        an exact path exists even when the feature is disabled or the ordinary
        session has no collaboration credential.
        """

        if not _is_collaboration_path(request.url.path):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        scheme, separator, token = authorization.partition(" ")
        bearer_present = (
            separator == " "
            and scheme.casefold() == "bearer"
            and bool(token.strip())
        )
        exact_route = False
        if not request.url.path.endswith("/"):
            for route in app.routes:
                route_path = str(getattr(route, "path", ""))
                if not _is_collaboration_path(route_path):
                    continue
                match, _ = route.matches(request.scope)
                if match is Match.FULL:
                    exact_route = True
                    break
        if (
            getattr(service, "enabled", True) is False
            or not bearer_present
            or not exact_route
        ):
            return _plain_not_found()
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def collaboration_validation_error(
        request: Request,
        error: RequestValidationError,
    ) -> Any:
        if not _is_collaboration_path(request.url.path):
            if previous_validation_handler is not None:
                return await previous_validation_handler(request, error)
            return await request_validation_exception_handler(request, error)
        return JSONResponse(
            status_code=422,
            content={
                "detail": {
                    "code": "invalid_collaboration_request",
                    "message": "request did not match the collaboration schema",
                    "errors": _safe_validation_errors(error),
                }
            },
        )

    bearer = HTTPBearer(auto_error=False)

    async def require_enabled() -> None:
        await _service_invoke(service.require_enabled)

    async def reject_query_secrets(request: Request) -> None:
        if any(
            key.casefold().replace("-", "_") in _FORBIDDEN_QUERY_KEYS
            for key in request.query_params
        ):
            raise _hidden_collaboration_error()

    async def user_principal(
        request: Request,
        _: None = Depends(require_enabled),
        __: None = Depends(reject_query_secrets),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> dict[str, str]:
        try:
            await _service_call(require_user_token(request, credentials))
        except HTTPException as error:
            if 400 <= error.status_code < 500:
                raise _hidden_collaboration_error() from error
            raise
        return {"role": "user", "sender_id": "local-user"}

    async def authenticated_lilies_principal(
        credentials: HTTPAuthorizationCredentials | None,
        *,
        channel_id: str,
        required_scope: str,
    ) -> Any:
        if credentials is None:
            raise _hidden_collaboration_error()
        parsed_channel_id = _hidden_uuid(channel_id)
        try:
            return await _service_invoke(
                service.authenticate_lilies,
                credentials.credentials,
                channel_id=parsed_channel_id,
                required_scope=required_scope,
            )
        except HTTPException as error:
            if 400 <= error.status_code < 500:
                raise _hidden_collaboration_error() from error
            raise

    async def lilies_reporter(
        channel_id: str,
        _: None = Depends(require_enabled),
        __: None = Depends(reject_query_secrets),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Any:
        return await authenticated_lilies_principal(
            credentials,
            channel_id=channel_id,
            required_scope="collaboration.report:write",
        )

    async def lilies_reader(
        channel_id: str,
        _: None = Depends(require_enabled),
        __: None = Depends(reject_query_secrets),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Any:
        return await authenticated_lilies_principal(
            credentials,
            channel_id=channel_id,
            required_scope="collaboration.response:read",
        )

    async def developer_principal(
        _: None = Depends(require_enabled),
        __: None = Depends(reject_query_secrets),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Any:
        if credentials is None:
            raise _hidden_collaboration_error()
        try:
            return await _service_invoke(
                service.authenticate_developer,
                credentials.credentials,
                required_scope="collaboration.developer",
            )
        except HTTPException as error:
            if 400 <= error.status_code < 500:
                raise _hidden_collaboration_error() from error
            raise

    async def verifier_principal(
        claim_id: str,
        _: None = Depends(require_enabled),
        __: None = Depends(reject_query_secrets),
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> Any:
        if credentials is None:
            raise _hidden_collaboration_error()
        parsed_claim_id = _hidden_uuid(claim_id)
        try:
            return await _service_invoke(
                service.authenticate_verifier,
                credentials.credentials,
                claim_id=parsed_claim_id,
                required_scope="collaboration.verify",
            )
        except HTTPException as error:
            if 400 <= error.status_code < 500:
                raise _hidden_collaboration_error() from error
            raise

    @app.get("/api/v1/collaboration/channels/{channel_id}")
    async def get_lilies_collaboration_channel_state(
        channel_id: str,
        principal: Any = Depends(lilies_reader),
    ) -> Any:
        return await _service_call(
            service.get_lilies_channel_state(
                principal=principal,
                channel_id=_hidden_uuid(channel_id),
            )
        )

    @app.post("/api/v1/collaboration/channels/{channel_id}/reports", status_code=201)
    async def submit_collaboration_report(
        channel_id: str,
        body: CollaborationReportSubmitRequest,
        principal: Any = Depends(lilies_reporter),
    ) -> Any:
        return await _service_call(
            service.submit_report(
                principal=principal,
                channel_id=_hidden_uuid(channel_id),
                request=body,
            )
        )

    @app.post(
        "/api/v1/collaboration/channels/{channel_id}/reports/{report_id}/evidence"
    )
    @app.post(
        "/api/v1/collaboration/channels/{channel_id}/reports/{report_id}/revisions"
    )
    async def revise_collaboration_report(
        channel_id: str,
        report_id: UUID,
        body: CollaborationReportRevisionRequest,
        principal: Any = Depends(lilies_reporter),
    ) -> Any:
        return await _service_call(
            service.revise_report(
                principal=principal,
                channel_id=_hidden_uuid(channel_id),
                report_id=report_id,
                request=body,
            )
        )

    @app.post(
        "/api/v1/collaboration/channels/{channel_id}/reports/{report_id}/withdrawals"
    )
    async def withdraw_collaboration_report(
        channel_id: str,
        report_id: UUID,
        body: CollaborationReportWithdrawalRequest,
        principal: Any = Depends(lilies_reporter),
    ) -> Any:
        return await _service_call(
            service.withdraw_report(
                principal=principal,
                channel_id=_hidden_uuid(channel_id),
                report_id=report_id,
                request=body,
            )
        )

    @app.get("/api/v1/collaboration/channels/{channel_id}/events")
    async def stream_lilies_collaboration_events(
        channel_id: str,
        request: Request,
        principal: Any = Depends(lilies_reader),
        after: int = Query(default=0, ge=0, le=_MAX_EVENT_CURSOR),
        response_format: Literal["sse", "json"] | None = Query(
            default=None,
            alias="format",
        ),
        limit: int = Query(default=100, ge=1, le=500),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> Any:
        parsed_channel_id = _hidden_uuid(channel_id)
        requested = _requested_cursor(after, last_event_id)
        cursor = await _service_call(
            service.resolve_event_cursor(
                principal=principal,
                channel_id=parsed_channel_id,
                requested_after=requested,
                durable=True,
            )
        )
        initial_events = await _service_call(
            service.list_events(
                principal=principal,
                channel_id=parsed_channel_id,
                after=cursor,
                limit=limit,
            )
        )
        accept = request.headers.get("accept", "").casefold()
        wants_json = response_format == "json" or (
            response_format is None
            and "application/json" in accept
            and "text/event-stream" not in accept
        )
        if wants_json:
            projected = [_model_dump(event) for event in initial_events]
            next_cursor = cursor
            for event in initial_events:
                next_cursor = max(next_cursor, _event_sequence(event))
            return {
                "channel_id": str(parsed_channel_id),
                "after": cursor,
                "next_cursor": next_cursor,
                "events": projected,
            }
        return StreamingResponse(
            _durable_event_stream(
                service=service,
                request=request,
                channel_id=parsed_channel_id,
                principal=principal,
                cursor=cursor,
                initial_events=initial_events,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, no-transform",
                "Last-Event-ID": str(cursor),
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/collaboration/channels/{channel_id}/acks")
    async def acknowledge_collaboration_events(
        channel_id: str,
        body: CollaborationAckRequest,
        principal: Any = Depends(lilies_reader),
    ) -> Any:
        return await _service_call(
            service.ack_events(
                principal=principal,
                channel_id=_hidden_uuid(channel_id),
                request=body,
            )
        )

    @app.post(
        "/api/v1/collaboration/channels/{channel_id}/verification-claims",
        status_code=201,
    )
    async def submit_collaboration_verification_claim(
        channel_id: str,
        body: CollaborationVerificationClaimRequest,
        principal: Any = Depends(lilies_reporter),
    ) -> Any:
        return await _service_call(
            service.submit_verification_claim(
                principal=principal,
                channel_id=_hidden_uuid(channel_id),
                request=body,
            )
        )

    @app.post(
        "/api/v1/collaboration/channels/{channel_id}/reports/{report_id}/reprobes"
    )
    async def submit_collaboration_reprobe(
        channel_id: str,
        report_id: UUID,
        body: CollaborationReprobeRequest,
        principal: Any = Depends(lilies_reporter),
    ) -> Any:
        return await _service_call(
            service.submit_lilies_reprobe(
                principal=principal,
                channel_id=_hidden_uuid(channel_id),
                report_id=report_id,
                request=body,
            )
        )

    @app.get("/api/v1/studio/collaboration/channels")
    async def list_studio_collaboration_channels(
        status_filter: ChannelStatus | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
        principal: Any = Depends(user_principal),
    ) -> Any:
        return await _service_call(
            service.list_channels(
                principal=principal,
                status=status_filter.value if status_filter is not None else None,
                limit=limit,
            )
        )

    @app.get("/api/v1/studio/collaboration/channels/{channel_id}")
    async def get_studio_collaboration_channel(
        channel_id: UUID,
        principal: Any = Depends(user_principal),
    ) -> Any:
        return await _service_call(
            service.get_channel_detail(principal=principal, channel_id=channel_id)
        )

    @app.get("/api/v1/studio/collaboration/channels/{channel_id}/export")
    async def export_studio_collaboration_channel(
        channel_id: UUID,
        principal: Any = Depends(user_principal),
    ) -> Any:
        return await _service_call(
            service.export_causal_chain(principal=principal, channel_id=channel_id)
        )

    @app.get("/api/v1/studio/collaboration/channels/{channel_id}/events")
    async def stream_studio_collaboration_events(
        channel_id: UUID,
        request: Request,
        principal: Any = Depends(user_principal),
        after: int = Query(default=0, ge=0, le=_MAX_EVENT_CURSOR),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        requested = _requested_cursor(after, last_event_id)
        cursor = await _service_call(
            service.resolve_event_cursor(
                principal=principal,
                channel_id=channel_id,
                requested_after=requested,
                durable=False,
            )
        )
        initial_events = await _service_call(
            service.list_events(
                principal=principal,
                channel_id=channel_id,
                after=cursor,
                limit=500,
            )
        )
        return StreamingResponse(
            _durable_event_stream(
                service=service,
                request=request,
                channel_id=channel_id,
                principal=principal,
                cursor=cursor,
                initial_events=initial_events,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store, no-transform",
                "Last-Event-ID": str(cursor),
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/v1/studio/collaboration/reports/{report_id}/decision")
    async def decide_studio_collaboration_report(
        report_id: UUID,
        body: CollaborationReportDecisionRequest,
        principal: Any = Depends(user_principal),
    ) -> Any:
        return await _service_call(
            service.decide_report(
                principal=principal,
                report_id=report_id,
                request=body,
            )
        )

    @app.patch("/api/v1/studio/collaboration/channels/{channel_id}/settings")
    async def update_studio_collaboration_settings(
        channel_id: UUID,
        body: CollaborationChannelSettingsRequest,
        principal: Any = Depends(user_principal),
    ) -> Any:
        return await _service_call(
            service.set_channel_approval_mode(
                principal=principal,
                channel_id=channel_id,
                request=body,
            )
        )

    @app.post("/api/v1/studio/collaboration/channels/{channel_id}/close")
    async def close_studio_collaboration_channel(
        channel_id: UUID,
        body: CollaborationChannelCloseRequest,
        principal: Any = Depends(user_principal),
    ) -> Any:
        return await _service_call(
            service.close_channel(
                principal=principal,
                channel_id=channel_id,
                request=body,
            )
        )

    @app.get("/api/v1/developer/collaboration/inbox")
    async def list_developer_collaboration_inbox(
        after: int = Query(default=0, ge=0, le=_MAX_EVENT_CURSOR),
        limit: int = Query(default=100, ge=1, le=500),
        route: ReportRoute | None = Query(default=None),
        principal: Any = Depends(developer_principal),
    ) -> Any:
        return await _service_call(
            service.developer_inbox(
                principal=principal,
                after=after,
                limit=limit,
                route=route.value if route is not None else None,
            )
        )

    @app.post("/api/v1/developer/collaboration/reports/{report_id}/lease")
    async def acquire_developer_collaboration_lease(
        report_id: UUID,
        body: CollaborationLeaseAcquireRequest,
        principal: Any = Depends(developer_principal),
    ) -> Any:
        return await _service_call(
            service.acquire_developer_lease(
                principal=principal,
                report_id=report_id,
                request=body,
            )
        )

    @app.post("/api/v1/developer/collaboration/reports/{report_id}/lease/renew")
    async def renew_developer_collaboration_lease(
        report_id: UUID,
        body: CollaborationLeaseRenewRequest,
        principal: Any = Depends(developer_principal),
    ) -> Any:
        return await _service_call(
            service.renew_developer_lease(
                principal=principal,
                report_id=report_id,
                request=body,
            )
        )

    @app.post("/api/v1/developer/collaboration/reports/{report_id}/responses")
    async def submit_developer_collaboration_response(
        report_id: UUID,
        body: CollaborationDeveloperResponseRequest,
        principal: Any = Depends(developer_principal),
    ) -> Any:
        return await _service_call(
            service.submit_developer_response(
                principal=principal,
                report_id=report_id,
                request=body,
            )
        )

    @app.post("/api/v1/developer/collaboration/reports/{report_id}/lease/release")
    async def release_developer_collaboration_lease(
        report_id: UUID,
        body: CollaborationLeaseReleaseRequest,
        principal: Any = Depends(developer_principal),
    ) -> Any:
        return await _service_call(
            service.release_developer_lease(
                principal=principal,
                report_id=report_id,
                request=body,
            )
        )

    @app.post("/api/v1/developer/collaboration/reports/{report_id}/task-amendments")
    async def submit_developer_task_amendment(
        report_id: UUID,
        body: CollaborationTaskAmendmentRequest,
        principal: Any = Depends(developer_principal),
    ) -> Any:
        return await _service_call(
            service.submit_task_amendment(
                principal=principal,
                report_id=report_id,
                request=body,
            )
        )

    @app.post("/api/v1/developer/collaboration/reports/{report_id}/environment-responses")
    async def submit_developer_environment_response(
        report_id: UUID,
        body: CollaborationEnvironmentResponseRequest,
        principal: Any = Depends(developer_principal),
    ) -> Any:
        return await _service_call(
            service.submit_environment_response(
                principal=principal,
                report_id=report_id,
                request=body,
            )
        )

    @app.post(
        "/api/v1/developer/collaboration/claims/{claim_id}/verification-results"
    )
    async def submit_independent_verification_result(
        claim_id: str,
        body: CollaborationVerificationResultRequest,
        principal: Any = Depends(verifier_principal),
    ) -> Any:
        return await _service_call(
            service.submit_verification_result(
                principal=principal,
                claim_id=_hidden_uuid(claim_id),
                request=body,
            )
        )

    # The temporary development channel is intentionally absent from the
    # ordinary product contract and public OpenAPI discovery surface.
    for route in app.routes:
        path = getattr(route, "path", "")
        if _is_collaboration_path(path):
            route.include_in_schema = False
    app.openapi_schema = None
