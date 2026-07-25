"""Platform-neutral HTTP adapter for collaborative software development."""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from starlette.routing import Match

from .collaborative_development_auth import (
    DevelopmentAuthenticationError,
    DevelopmentCredentialIssuer,
    DevelopmentPrincipal,
)
from .collaborative_development_models import (
    AgentRole,
    ApprovalMode,
    DevelopmentAssignment,
    DevelopmentBudget,
    DevelopmentResult,
    DevelopmentWorkItem,
    ExecutionMode,
    LiliesReview,
    WorkspaceGrant,
)
from .collaborative_development_service import (
    CollaborativeDevelopmentError,
    CollaborativeDevelopmentService,
)
from .development_workspace_broker import DevelopmentReviewSnapshotReceipt


_PREFIX = "/api/v1/collaborative-development/"
_FORBIDDEN_QUERY_KEYS = frozenset(
    {
        "token",
        "api_token",
        "access_token",
        "authorization",
        "cookie",
        "password",
        "secret",
    }
)
UserTokenValidator = Callable[[str], bool | Awaitable[bool]]


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AssignmentCreateRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    assignment: DevelopmentAssignment


class AssignmentModeRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    expected_revision: int = Field(ge=1)
    mode: ExecutionMode


class AssignmentApprovalModeRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    expected_revision: int = Field(ge=1)
    mode: ApprovalMode


class AuthorityRejectionRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    reason: str = Field(min_length=1, max_length=2_000)


class AuthorityApprovalRequest(AuthorityRejectionRequest):
    expected_assignment_revision: int = Field(ge=1)
    replacement_grant: WorkspaceGrant
    replacement_budget: DevelopmentBudget | None = None


class WorkItemCreateRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    work_item: DevelopmentWorkItem


class RevisionRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    expected_revision: int = Field(ge=1)


class LeaseAcquireRequest(RevisionRequest):
    ttl_seconds: int = Field(default=900, ge=1, le=3_600)


class WorkStartRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    expected_work_item_revision: int = Field(ge=1)


class ResultSubmitRequest(WorkStartRequest):
    result: DevelopmentResult


class ReviewSubmitRequest(WorkStartRequest):
    review: LiliesReview


class ReviewReconciliationRequeueRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    expected_work_item_revision: int = Field(ge=1)
    expected_failed_attempt: int = Field(ge=1)
    confirmation: Literal["requeue_unknown_review_attempt"]
    reason: str = Field(min_length=1, max_length=2_000)


class ReviewSnapshotPrepareRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)


class ReaderAckRequest(StrictRequest):
    idempotency_key: str = Field(min_length=8, max_length=240)
    ack_seq: int = Field(ge=0)
    expected_cursor_revision: int = Field(ge=0)


class AssignmentCreatedResponse(StrictRequest):
    assignment: DevelopmentAssignment
    lilies_access_token: str
    codex_access_token: str
    enterprise_denominator: bool = False


class DevelopmentResultReadResponse(StrictRequest):
    result: DevelopmentResult
    enterprise_denominator: Literal[False] = False


class ReviewSnapshotPreparedResponse(StrictRequest):
    review_snapshot: DevelopmentReviewSnapshotReceipt
    enterprise_denominator: Literal[False] = False


async def _resolve_bool(result: bool | Awaitable[bool]) -> bool:
    if hasattr(result, "__await__"):
        return bool(await result)  # type: ignore[misc]
    return bool(result)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "code": "collaborative_development_not_found",
            "message": "resource was not found",
        },
    )


def _model_dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_model_dump(item) for item in value]
    if isinstance(value, tuple):
        return [_model_dump(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _model_dump(item) for key, item in value.items()}
    return value


def install_collaborative_development_api(
    app: FastAPI,
    service: CollaborativeDevelopmentService,
    *,
    credential_issuer: DevelopmentCredentialIssuer,
    user_token_validator: UserTokenValidator,
    include_in_schema: bool,
) -> None:
    """Install an adapter that has no workflow-platform dependencies."""

    bearer = HTTPBearer(auto_error=False)
    route_get = partial(app.get, include_in_schema=include_in_schema)
    route_post = partial(app.post, include_in_schema=include_in_schema)

    @app.middleware("http")
    async def hide_collaborative_development_topology(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        if not request.url.path.startswith(_PREFIX):
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        scheme, separator, value = authorization.partition(" ")
        exact_route = False
        if not request.url.path.endswith("/"):
            for route in app.routes:
                route_path = str(getattr(route, "path", ""))
                if not route_path.startswith(_PREFIX):
                    continue
                match, _ = route.matches(request.scope)
                if match is Match.FULL:
                    exact_route = True
                    break
        if (
            not service.enabled
            or separator != " "
            or scheme.casefold() != "bearer"
            or not value.strip()
            or not exact_route
        ):
            return PlainTextResponse("Not Found", status_code=404)
        return await call_next(request)

    async def principal(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> DevelopmentPrincipal:
        if any(
            key.casefold().replace("-", "_") in _FORBIDDEN_QUERY_KEYS
            for key in request.query_params
        ):
            raise _not_found()
        if credentials is None:
            raise _not_found()
        supplied = credentials.credentials
        if await _resolve_bool(user_token_validator(supplied)):
            return DevelopmentPrincipal(
                actor_role="user",
                actor_id="local-user",
            )
        try:
            parsed = credential_issuer.authenticate(supplied)
            await service.validate_principal(parsed)
            return parsed
        except (
            CollaborativeDevelopmentError,
            DevelopmentAuthenticationError,
        ) as error:
            raise _not_found() from error

    @app.exception_handler(CollaborativeDevelopmentError)
    async def collaborative_development_error(
        request: Request,
        error: CollaborativeDevelopmentError,
    ) -> JSONResponse:
        if not request.url.path.startswith(_PREFIX):
            raise error
        return JSONResponse(
            status_code=error.status_code,
            content={"detail": error.public_detail()},
        )

    @route_post(f"{_PREFIX}assignments", status_code=201)
    async def create_assignment(
        body: AssignmentCreateRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        created = await service.create_assignment(
            principal=actor,
            assignment=body.assignment,
            idempotency_key=body.idempotency_key,
        )
        return AssignmentCreatedResponse(
            assignment=created,
            lilies_access_token=credential_issuer.issue(
                created.assignment_id,
                AgentRole.lilies,
            ),
            codex_access_token=credential_issuer.issue(
                created.assignment_id,
                AgentRole.codex,
            ),
        )

    @route_get(f"{_PREFIX}assignments/{{assignment_id}}")
    async def get_assignment(
        assignment_id: UUID,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.get_assignment(
            principal=actor,
            assignment_id=assignment_id,
        )

    @route_get(f"{_PREFIX}assignments/{{assignment_id}}/status")
    async def get_assignment_status(
        assignment_id: UUID,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.status_summary(
            principal=actor,
            assignment_id=assignment_id,
        )

    @route_get(
        f"{_PREFIX}assignments/{{assignment_id}}/review-reconciliations"
    )
    async def list_review_reconciliations(
        assignment_id: UUID,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.list_review_reconciliations(
            principal=actor,
            assignment_id=assignment_id,
        )

    @route_post(
        f"{_PREFIX}assignments/{{assignment_id}}/review-reconciliations/"
        "{outbox_id}/requeue"
    )
    async def requeue_review_reconciliation(
        assignment_id: UUID,
        outbox_id: UUID,
        body: ReviewReconciliationRequeueRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.requeue_review_reconciliation(
            principal=actor,
            assignment_id=assignment_id,
            outbox_id=outbox_id,
            expected_work_item_revision=body.expected_work_item_revision,
            expected_failed_attempt=body.expected_failed_attempt,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )

    @route_get(f"{_PREFIX}assignments/{{assignment_id}}/authority-requests")
    async def list_authority_requests(
        assignment_id: UUID,
        status: Literal["pending", "approved", "rejected", "all"] = Query(
            default="pending"
        ),
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        requests = await service.list_authority_requests(
            principal=actor,
            assignment_id=assignment_id,
            status=status,
        )
        return {
            "assignment_id": str(assignment_id),
            "status": status,
            "requests": _model_dump(requests),
            "enterprise_denominator": False,
        }

    @route_post(
        f"{_PREFIX}assignments/{{assignment_id}}/"
        "authority-requests/{request_id}/approve"
    )
    async def approve_authority_request(
        assignment_id: UUID,
        request_id: UUID,
        body: AuthorityApprovalRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.approve_authority_request(
            principal=actor,
            assignment_id=assignment_id,
            request_id=request_id,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
            expected_assignment_revision=body.expected_assignment_revision,
            replacement_grant=body.replacement_grant,
            replacement_budget=body.replacement_budget,
        )

    @route_post(
        f"{_PREFIX}assignments/{{assignment_id}}/"
        "authority-requests/{request_id}/reject"
    )
    async def reject_authority_request(
        assignment_id: UUID,
        request_id: UUID,
        body: AuthorityRejectionRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.reject_authority_request(
            principal=actor,
            assignment_id=assignment_id,
            request_id=request_id,
            reason=body.reason,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}assignments/{{assignment_id}}/execution-mode")
    async def set_execution_mode(
        assignment_id: UUID,
        body: AssignmentModeRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.set_execution_mode(
            principal=actor,
            assignment_id=assignment_id,
            mode=body.mode,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}assignments/{{assignment_id}}/approval-mode")
    async def set_approval_mode(
        assignment_id: UUID,
        body: AssignmentApprovalModeRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.set_approval_mode(
            principal=actor,
            assignment_id=assignment_id,
            mode=body.mode,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}assignments/{{assignment_id}}/stop")
    async def stop_assignment(
        assignment_id: UUID,
        body: RevisionRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.stop_assignment(
            principal=actor,
            assignment_id=assignment_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}assignments/{{assignment_id}}/archive")
    async def archive_assignment(
        assignment_id: UUID,
        body: RevisionRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.archive_assignment(
            principal=actor,
            assignment_id=assignment_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_post(
        f"{_PREFIX}assignments/{{assignment_id}}/work-items",
        status_code=201,
    )
    async def create_work_item(
        assignment_id: UUID,
        body: WorkItemCreateRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        if body.work_item.assignment_id != assignment_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "collaborative_development_conflict",
                    "message": "work item assignment binding does not match the route",
                },
            )
        return await service.create_work_item(
            principal=actor,
            item=body.work_item,
            idempotency_key=body.idempotency_key,
        )

    @route_get(f"{_PREFIX}assignments/{{assignment_id}}/work-items")
    async def list_work_items(
        assignment_id: UUID,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.list_work_items(
            principal=actor,
            assignment_id=assignment_id,
        )

    @route_post(f"{_PREFIX}work-items/{{work_item_id}}/dispatch")
    async def dispatch_work_item(
        work_item_id: UUID,
        body: RevisionRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.dispatch_work_item(
            principal=actor,
            work_item_id=work_item_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}work-items/{{work_item_id}}/lease")
    async def acquire_lease(
        work_item_id: UUID,
        body: LeaseAcquireRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.acquire_lease(
            principal=actor,
            work_item_id=work_item_id,
            expected_revision=body.expected_revision,
            ttl_seconds=body.ttl_seconds,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}leases/{{lease_id}}/start")
    async def start_work(
        lease_id: UUID,
        body: WorkStartRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.start_work(
            principal=actor,
            lease_id=lease_id,
            expected_work_item_revision=body.expected_work_item_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}work-items/{{work_item_id}}/results")
    async def submit_result(
        work_item_id: UUID,
        body: ResultSubmitRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        if body.result.work_item_id != work_item_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "collaborative_development_conflict",
                    "message": "result work item binding does not match the route",
                },
            )
        return await service.submit_result(
            principal=actor,
            result=body.result,
            expected_work_item_revision=body.expected_work_item_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_get(f"{_PREFIX}results/{{result_id}}")
    async def get_result(
        result_id: UUID,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        result = await service.get_result(
            principal=actor,
            result_id=result_id,
        )
        return DevelopmentResultReadResponse(result=result)

    @route_post(f"{_PREFIX}results/{{result_id}}/review-snapshot")
    async def prepare_review_snapshot(
        result_id: UUID,
        body: ReviewSnapshotPrepareRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        receipt = await service.prepare_review_snapshot(
            principal=actor,
            result_id=result_id,
            idempotency_key=body.idempotency_key,
        )
        return ReviewSnapshotPreparedResponse(review_snapshot=receipt)

    @route_post(f"{_PREFIX}work-items/{{work_item_id}}/reviews")
    async def submit_review(
        work_item_id: UUID,
        body: ReviewSubmitRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        if body.review.work_item_id != work_item_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "collaborative_development_conflict",
                    "message": "review work item binding does not match the route",
                },
            )
        return await service.submit_review(
            principal=actor,
            review=body.review,
            expected_work_item_revision=body.expected_work_item_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_post(f"{_PREFIX}work-items/{{work_item_id}}/close")
    async def close_work_item(
        work_item_id: UUID,
        body: RevisionRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.close_work_item(
            principal=actor,
            work_item_id=work_item_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_get(f"{_PREFIX}assignments/{{assignment_id}}/events")
    async def read_events(
        assignment_id: UUID,
        request: Request,
        after: int | None = Query(default=None, ge=0),
        limit: int = Query(default=1_000, ge=1, le=5_000),
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        last_event_id = request.headers.get("last-event-id")
        header_after: int | None = None
        if last_event_id is not None:
            try:
                header_after = int(last_event_id)
            except ValueError as error:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "invalid_event_cursor",
                        "message": "Last-Event-ID must be a non-negative integer",
                    },
                ) from error
            if header_after < 0:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "invalid_event_cursor",
                        "message": "Last-Event-ID must be a non-negative integer",
                    },
                )
        if after is not None and header_after is not None and after != header_after:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "event_cursor_conflict",
                    "message": "after and Last-Event-ID must identify the same cursor",
                },
            )
        effective_after = after if after is not None else header_after
        if effective_after is None:
            cursor = await service.reader_cursor(
                principal=actor,
                assignment_id=assignment_id,
            )
            effective_after = cursor.ack_seq if cursor is not None else 0
        events = await service.read_events(
            principal=actor,
            assignment_id=assignment_id,
            after=effective_after,
            limit=limit,
        )
        next_cursor = max([effective_after, *(event.seq for event in events)])
        accepted = {
            value.split(";", 1)[0].strip().casefold()
            for value in request.headers.get("accept", "").split(",")
        }
        if "text/event-stream" in accepted:
            def encoded_events() -> Any:
                for event in events:
                    payload = json.dumps(
                        _model_dump(event),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    yield (
                        f"id: {event.seq}\n"
                        f"event: {event.event_type}\n"
                        f"data: {payload}\n\n"
                    )
                yield f": durable-cursor {next_cursor}\nretry: 1000\n\n"

            return StreamingResponse(
                encoded_events(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-store",
                    "X-Accel-Buffering": "no",
                },
            )
        return {
            "assignment_id": str(assignment_id),
            "after": effective_after,
            "next_cursor": next_cursor,
            "events": _model_dump(events),
        }

    @route_post(f"{_PREFIX}assignments/{{assignment_id}}/acks")
    async def ack_events(
        assignment_id: UUID,
        body: ReaderAckRequest,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.ack_events(
            principal=actor,
            assignment_id=assignment_id,
            ack_seq=body.ack_seq,
            expected_cursor_revision=body.expected_cursor_revision,
            idempotency_key=body.idempotency_key,
        )

    @route_get(f"{_PREFIX}assignments/{{assignment_id}}/workspace-authority")
    async def get_workspace_authority(
        assignment_id: UUID,
        actor: DevelopmentPrincipal = Depends(principal),
    ) -> Any:
        return await service.workspace_authority(
            principal=actor,
            assignment_id=assignment_id,
        )


def create_standalone_collaborative_development_app(
    *,
    service: CollaborativeDevelopmentService,
    credential_issuer: DevelopmentCredentialIssuer,
    owner_token: str,
) -> FastAPI:
    """Create the same API without importing or starting the workflow platform."""

    if len(owner_token) < 32:
        raise ValueError("standalone collaborative development owner token is too short")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await service.initialize()
        yield

    app = FastAPI(
        title="Lilies Collaborative Development",
        version="0.4.13",
        lifespan=lifespan,
    )

    def validate_owner(supplied: str) -> bool:
        return hmac.compare_digest(owner_token, supplied)

    install_collaborative_development_api(
        app,
        service,
        credential_issuer=credential_issuer,
        user_token_validator=validate_owner,
        include_in_schema=True,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "collaborative-development",
            "enabled": service.enabled,
            "autonomous_enabled": service.autonomous_enabled,
            "workflow_platform_required": False,
            "enterprise_denominator": False,
        }

    return app


__all__ = [
    "AssignmentApprovalModeRequest",
    "AssignmentCreateRequest",
    "AssignmentCreatedResponse",
    "AssignmentModeRequest",
    "AuthorityApprovalRequest",
    "AuthorityRejectionRequest",
    "LeaseAcquireRequest",
    "ReaderAckRequest",
    "DevelopmentResultReadResponse",
    "ResultSubmitRequest",
    "ReviewSnapshotPrepareRequest",
    "ReviewSnapshotPreparedResponse",
    "ReviewSubmitRequest",
    "RevisionRequest",
    "WorkItemCreateRequest",
    "WorkStartRequest",
    "create_standalone_collaborative_development_app",
    "install_collaborative_development_api",
]
