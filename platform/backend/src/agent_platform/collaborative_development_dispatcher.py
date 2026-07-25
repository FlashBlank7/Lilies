"""Durable dispatcher for manual-approved or autonomous agent handoffs."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sqlite3
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field

from .collaborative_development_handler import RoleBoundDispatchContext
from .collaborative_development_models import (
    AgentRole,
    AssignmentStatus,
    DevelopmentAssignment,
    DevelopmentBudget,
    DevelopmentWorkItem,
    DispatchOutboxItem,
    ExecutionMode,
    SideEffect,
    WorkItemStatus,
    WorkspaceGrant,
    utc_now,
)
from .collaborative_development_storage import (
    CollaborativeDevelopmentConflict,
    CollaborativeDevelopmentStore,
)


_DISPATCH_NAMESPACE = UUID("a22e05c7-f960-4f4b-b18b-dd79b97238b1")


def canonical_digest(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class DispatchOutcomeStatus(str, Enum):
    delivered = "delivered"
    authorization_required = "authorization_required"
    retry = "retry"
    reconciliation_required = "reconciliation_required"


class RequestedAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    paths: tuple[str, ...] = ()
    argv: tuple[tuple[str, ...], ...] = ()
    hosts: tuple[str, ...] = ()
    side_effects: tuple[SideEffect, ...] = ()
    secret_refs: tuple[str, ...] = ()
    budget: DevelopmentBudget | None = None
    reason: str = Field(min_length=1, max_length=20_000)


class DispatchOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: DispatchOutcomeStatus
    evidence_refs: tuple[str, ...] = ()
    detail: str = Field(min_length=1, max_length=20_000)
    requested_authority: RequestedAuthority | None = None
    retry_after_seconds: int | None = Field(default=None, ge=1, le=3_600)


class DevelopmentAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    assignment_id: UUID
    work_item_id: UUID
    outbox_id: UUID
    destination_role: AgentRole
    existing_grant_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_authority: RequestedAuthority
    status: Literal["pending"] = "pending"
    created_at: datetime


class DispatchHistoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    dispatch_id: UUID
    outbox_id: UUID
    attempt: int = Field(ge=1)
    assignment_id: UUID
    work_item_id: UUID
    destination_role: AgentRole
    outbox_kind: str
    execution_mode: ExecutionMode
    grant_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: DispatchOutcomeStatus
    evidence_refs: tuple[str, ...] = ()
    invocation_fence_id: UUID | None = None
    authorization_request_id: UUID | None = None
    retry_after_seconds: int | None = Field(default=None, ge=1, le=3_600)
    detail: str
    created_at: datetime


class DispatchInvocationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    outbox_id: UUID
    attempt: int = Field(ge=1)
    claim_id: UUID
    assignment_id: UUID
    work_item_id: UUID
    state: Literal["started", "completed"]
    started_at: datetime
    completed_at: datetime | None = None


class DevelopmentDispatchHandler(Protocol):
    def __call__(
        self,
        *,
        context: RoleBoundDispatchContext,
    ) -> DispatchOutcome | Awaitable[DispatchOutcome]: ...


class CollaborativeDevelopmentDispatchJournal:
    """Small independent journal so dispatcher crashes cannot erase decisions."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().resolve()

    def _tighten_database_files(self) -> None:
        self.database_path.parent.chmod(0o700)
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database_path.parent.chmod(0o700)
        connection = sqlite3.connect(self.database_path)
        try:
            self.database_path.chmod(0o600)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS development_dispatch_history (
                  dispatch_id TEXT PRIMARY KEY,
                  outbox_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL CHECK(attempt >= 1),
                  assignment_id TEXT NOT NULL,
                  work_item_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(outbox_id,attempt)
                );
                CREATE TABLE IF NOT EXISTS development_authorization_requests (
                  request_id TEXT PRIMARY KEY,
                  outbox_id TEXT NOT NULL UNIQUE,
                  assignment_id TEXT NOT NULL,
                  work_item_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS development_dispatch_invocations (
                  outbox_id TEXT NOT NULL,
                  attempt INTEGER NOT NULL CHECK(attempt >= 1),
                  claim_id TEXT NOT NULL,
                  assignment_id TEXT NOT NULL,
                  work_item_id TEXT NOT NULL,
                  state TEXT NOT NULL CHECK(state IN ('started','completed')),
                  payload_json TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  completed_at TEXT,
                  PRIMARY KEY(outbox_id,attempt)
                );
                """
            )
            connection.commit()
            self._tighten_database_files()
        finally:
            connection.close()
            self._tighten_database_files()

    def begin_invocation(
        self,
        invocation: DispatchInvocationRecord,
    ) -> bool:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT 1 FROM development_dispatch_invocations
                WHERE outbox_id=? AND attempt=?
                """,
                (str(invocation.outbox_id), invocation.attempt),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO development_dispatch_invocations(
                  outbox_id,attempt,claim_id,assignment_id,work_item_id,state,
                  payload_json,started_at,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    str(invocation.outbox_id),
                    invocation.attempt,
                    str(invocation.claim_id),
                    str(invocation.assignment_id),
                    str(invocation.work_item_id),
                    invocation.state,
                    invocation.model_dump_json(),
                    invocation.started_at.isoformat(),
                ),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._tighten_database_files()

    def invocation(
        self,
        outbox_id: UUID,
        attempt: int,
    ) -> DispatchInvocationRecord | None:
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                """
                SELECT payload_json FROM development_dispatch_invocations
                WHERE outbox_id=? AND attempt=?
                """,
                (str(outbox_id), attempt),
            ).fetchone()
            return (
                DispatchInvocationRecord.model_validate_json(str(row[0]))
                if row is not None
                else None
            )
        finally:
            connection.close()
            self._tighten_database_files()

    def history_for_outbox(
        self,
        outbox_id: UUID,
        attempt: int,
    ) -> DispatchHistoryRecord | None:
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                """
                SELECT payload_json FROM development_dispatch_history
                WHERE outbox_id=? AND attempt=?
                """,
                (str(outbox_id), attempt),
            ).fetchone()
            return (
                DispatchHistoryRecord.model_validate_json(str(row[0]))
                if row is not None
                else None
            )
        finally:
            connection.close()
            self._tighten_database_files()

    def record(
        self,
        history: DispatchHistoryRecord,
        authorization_request: DevelopmentAuthorizationRequest | None,
    ) -> DispatchHistoryRecord:
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT payload_json FROM development_dispatch_history
                WHERE outbox_id=? AND attempt=?
                """,
                (str(history.outbox_id), history.attempt),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return DispatchHistoryRecord.model_validate_json(str(existing[0]))
            if authorization_request is not None:
                connection.execute(
                    """
                    INSERT INTO development_authorization_requests(
                      request_id,outbox_id,assignment_id,work_item_id,status,
                      payload_json,created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        str(authorization_request.request_id),
                        str(authorization_request.outbox_id),
                        str(authorization_request.assignment_id),
                        str(authorization_request.work_item_id),
                        authorization_request.status,
                        authorization_request.model_dump_json(),
                        authorization_request.created_at.isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO development_dispatch_history(
                  dispatch_id,outbox_id,attempt,assignment_id,work_item_id,status,
                  payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(history.dispatch_id),
                    str(history.outbox_id),
                    history.attempt,
                    str(history.assignment_id),
                    str(history.work_item_id),
                    history.status.value,
                    history.model_dump_json(),
                    history.created_at.isoformat(),
                ),
            )
            completed = DispatchInvocationRecord(
                outbox_id=history.outbox_id,
                attempt=history.attempt,
                claim_id=UUID(
                    str(
                        connection.execute(
                            """
                            SELECT claim_id FROM development_dispatch_invocations
                            WHERE outbox_id=? AND attempt=?
                            """,
                            (str(history.outbox_id), history.attempt),
                        ).fetchone()[0]
                    )
                ),
                assignment_id=history.assignment_id,
                work_item_id=history.work_item_id,
                state="completed",
                started_at=DispatchInvocationRecord.model_validate_json(
                    str(
                        connection.execute(
                            """
                            SELECT payload_json FROM development_dispatch_invocations
                            WHERE outbox_id=? AND attempt=?
                            """,
                            (str(history.outbox_id), history.attempt),
                        ).fetchone()[0]
                    )
                ).started_at,
                completed_at=history.created_at,
            )
            connection.execute(
                """
                UPDATE development_dispatch_invocations
                SET state='completed',payload_json=?,completed_at=?
                WHERE outbox_id=? AND attempt=?
                """,
                (
                    completed.model_dump_json(),
                    history.created_at.isoformat(),
                    str(history.outbox_id),
                    history.attempt,
                ),
            )
            connection.commit()
            return history
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._tighten_database_files()

    def history(self, assignment_id: UUID) -> list[DispatchHistoryRecord]:
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT payload_json FROM development_dispatch_history
                WHERE assignment_id=? ORDER BY created_at,dispatch_id
                """,
                (str(assignment_id),),
            ).fetchall()
            return [
                DispatchHistoryRecord.model_validate_json(str(row[0])) for row in rows
            ]
        finally:
            connection.close()
            self._tighten_database_files()

    def authorization_requests(
        self,
        assignment_id: UUID,
    ) -> list[DevelopmentAuthorizationRequest]:
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                """
                SELECT payload_json FROM development_authorization_requests
                WHERE assignment_id=? ORDER BY created_at,request_id
                """,
                (str(assignment_id),),
            ).fetchall()
            return [
                DevelopmentAuthorizationRequest.model_validate_json(str(row[0]))
                for row in rows
            ]
        finally:
            connection.close()
            self._tighten_database_files()


class CollaborativeDevelopmentDispatcher:
    """Deliver durable outbox work without changing its frozen authority."""

    def __init__(
        self,
        *,
        store: CollaborativeDevelopmentStore,
        journal: CollaborativeDevelopmentDispatchJournal,
        handlers: Mapping[AgentRole, DevelopmentDispatchHandler],
        dispatcher_id: str | None = None,
        claim_ttl_seconds: int = 900,
    ) -> None:
        self.store = store
        self.journal = journal
        self.handlers = dict(handlers)
        self.dispatcher_id = dispatcher_id or f"dispatcher-{uuid4().hex}"
        if not 1 <= claim_ttl_seconds <= 86_400:
            raise ValueError("claim TTL must be between 1 and 86400 seconds")
        self.claim_ttl_seconds = claim_ttl_seconds

    async def initialize(self) -> None:
        await self.store.initialize()
        await asyncio.to_thread(self.journal.initialize)

    @staticmethod
    def _grant(
        assignment: DevelopmentAssignment,
        role: AgentRole,
    ) -> WorkspaceGrant:
        matches = [
            grant for grant in assignment.workspace_grants if grant.agent_role == role
        ]
        if len(matches) != 1:
            raise RuntimeError("assignment does not contain one exact destination grant")
        return matches[0]

    @staticmethod
    def _validate_authority_request(
        grant: WorkspaceGrant,
        budget: DevelopmentBudget,
        request: RequestedAuthority,
    ) -> None:
        """Reject fake requests that are already inside the frozen grant."""

        missing_path = any(path not in grant.allowed_paths for path in request.paths)
        missing_argv = any(argv not in grant.allowed_argv for argv in request.argv)
        missing_host = any(host not in grant.allowed_hosts for host in request.hosts)
        missing_effect = any(
            effect not in grant.allowed_side_effects for effect in request.side_effects
        )
        missing_secret = any(
            secret not in grant.secret_refs for secret in request.secret_refs
        )
        budget_expansion = (
            request.budget is not None
            and (
                request.budget.max_work_items > budget.max_work_items
                or request.budget.max_commands > budget.max_commands
                or request.budget.max_tool_calls > budget.max_tool_calls
                or request.budget.max_wall_seconds > budget.max_wall_seconds
                or request.budget.max_cost_usd > budget.max_cost_usd
            )
        )
        if not any(
            (
                missing_path,
                missing_argv,
                missing_host,
                missing_effect,
                missing_secret,
                budget_expansion,
            )
        ):
            raise RuntimeError(
                "authorization_required outcome did not request authority outside the grant"
            )

    async def _invoke(
        self,
        handler: DevelopmentDispatchHandler,
        *,
        outbox: DispatchOutboxItem,
        assignment: DevelopmentAssignment,
        work_item: DevelopmentWorkItem,
        grant: WorkspaceGrant,
    ) -> DispatchOutcome:
        context = RoleBoundDispatchContext.from_assignment(
            outbox=outbox,
            assignment=assignment,
            work_item=work_item,
            workspace_grant=grant,
        )
        if inspect.iscoroutinefunction(handler):
            outcome = await handler(context=context)
        else:
            outcome = await asyncio.to_thread(handler, context=context)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        return DispatchOutcome.model_validate(outcome)

    async def _heartbeat_claim(self, claim_id: UUID, outbox_id: UUID) -> None:
        interval = max(0.1, min(float(self.claim_ttl_seconds) / 3.0, 5.0))
        while True:
            await asyncio.sleep(interval)
            await self.store.renew_outbox_claim(
                outbox_id,
                claim_id=claim_id,
                extend_seconds=self.claim_ttl_seconds,
            )

    async def _invoke_with_heartbeat(
        self,
        handler: DevelopmentDispatchHandler,
        *,
        claim_id: UUID,
        outbox: DispatchOutboxItem,
        assignment: DevelopmentAssignment,
        work_item: DevelopmentWorkItem,
        grant: WorkspaceGrant,
    ) -> DispatchOutcome:
        heartbeat = asyncio.create_task(
            self._heartbeat_claim(claim_id, outbox.outbox_id)
        )
        try:
            return await self._invoke(
                handler,
                outbox=outbox,
                assignment=assignment,
                work_item=work_item,
                grant=grant,
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    @staticmethod
    def _history(
        *,
        outbox: DispatchOutboxItem,
        attempt: int,
        assignment: DevelopmentAssignment,
        grant_digest: str,
        outcome: DispatchOutcome,
        authorization_request: DevelopmentAuthorizationRequest | None,
        now: datetime,
        invocation_fence_id: UUID | None = None,
    ) -> DispatchHistoryRecord:
        return DispatchHistoryRecord(
            dispatch_id=uuid5(
                _DISPATCH_NAMESPACE,
                f"dispatch:{outbox.outbox_id}:{attempt}",
            ),
            outbox_id=outbox.outbox_id,
            attempt=attempt,
            assignment_id=outbox.assignment_id,
            work_item_id=outbox.work_item_id,
            destination_role=outbox.destination_role,
            outbox_kind=outbox.kind,
            execution_mode=assignment.execution_mode,
            grant_digest=grant_digest,
            status=outcome.status,
            evidence_refs=outcome.evidence_refs,
            invocation_fence_id=invocation_fence_id,
            authorization_request_id=(
                authorization_request.request_id
                if authorization_request is not None
                else None
            ),
            retry_after_seconds=outcome.retry_after_seconds,
            detail=outcome.detail,
            created_at=now,
        )

    async def _finalize(
        self,
        history: DispatchHistoryRecord,
        *,
        claim_id: UUID,
    ) -> None:
        assignment = await self.store.get_assignment(history.assignment_id)
        if assignment.status != AssignmentStatus.active:
            # stop_assignment already revoked the lease and durably cancelled
            # the outbox.  A handler that observed cancellation must never
            # resurrect or overwrite that terminal user boundary.
            return
        if assignment.deadline <= utc_now():
            await self.store.mark_outbox_failed(
                history.outbox_id,
                claim_id=claim_id,
                error="assignment_deadline_reached",
                retry_at=None,
            )
            return
        if history.status == DispatchOutcomeStatus.delivered:
            await self.store.mark_outbox_delivered(
                history.outbox_id,
                claim_id=claim_id,
                delivered_at=utc_now(),
            )
            return
        if history.status == DispatchOutcomeStatus.retry:
            if history.retry_after_seconds is None:
                raise RuntimeError("retry history requires retry_after_seconds")
            retry_at = utc_now() + timedelta(seconds=history.retry_after_seconds)
        else:
            retry_at = None
        await self.store.mark_outbox_failed(
            history.outbox_id,
            claim_id=claim_id,
            error=history.status.value,
            retry_at=retry_at,
        )

    @staticmethod
    def _invocation_restriction(
        assignment: DevelopmentAssignment,
        *,
        now: datetime,
    ) -> str | None:
        if assignment.status != AssignmentStatus.active:
            return "assignment is not active"
        if assignment.deadline <= now:
            return "assignment deadline has been reached"
        return None

    async def _dependencies_accepted(
        self,
        work_item: DevelopmentWorkItem,
    ) -> bool:
        for dependency_id in work_item.dependencies:
            dependency = await self.store.get_work_item(dependency_id)
            if dependency.status not in {
                WorkItemStatus.accepted,
                WorkItemStatus.closed,
            }:
                return False
        return True

    async def dispatch_once(self, *, limit: int = 100) -> list[DispatchHistoryRecord]:
        claims = await self.store.claim_pending_outbox(
            claimed_by=self.dispatcher_id,
            claim_ttl_seconds=self.claim_ttl_seconds,
            limit=limit,
        )
        records: list[DispatchHistoryRecord] = []
        for claim in claims:
            outbox = claim.outbox
            attempt = outbox.attempts + 1
            assignment = await self.store.get_assignment(outbox.assignment_id)
            work_item = await self.store.get_work_item(outbox.work_item_id)
            grant = self._grant(assignment, outbox.destination_role)
            grant_digest = canonical_digest(grant)
            completed = await asyncio.to_thread(
                self.journal.history_for_outbox,
                outbox.outbox_id,
                attempt,
            )
            if completed is not None:
                await self._finalize(completed, claim_id=claim.claim_id)
                records.append(completed)
                continue
            invocation = DispatchInvocationRecord(
                outbox_id=outbox.outbox_id,
                attempt=attempt,
                claim_id=claim.claim_id,
                assignment_id=outbox.assignment_id,
                work_item_id=outbox.work_item_id,
                state="started",
                started_at=utc_now(),
            )
            began = await asyncio.to_thread(
                self.journal.begin_invocation,
                invocation,
            )
            if not began:
                outcome = DispatchOutcome(
                    status=DispatchOutcomeStatus.reconciliation_required,
                    detail=(
                        "A prior dispatcher began this external handoff but did not "
                        "durably record its outcome; automatic replay is stopped to "
                        "avoid a duplicate side effect."
                    ),
                )
                history = self._history(
                    outbox=outbox,
                    attempt=attempt,
                    assignment=assignment,
                    grant_digest=grant_digest,
                    outcome=outcome,
                    authorization_request=None,
                    now=utc_now(),
                )
                persisted = await asyncio.to_thread(
                    self.journal.record,
                    history,
                    None,
                )
                await self._finalize(persisted, claim_id=claim.claim_id)
                records.append(persisted)
                continue
            assignment = await self.store.get_assignment(outbox.assignment_id)
            work_item = await self.store.get_work_item(outbox.work_item_id)
            grant = self._grant(assignment, outbox.destination_role)
            grant_digest = canonical_digest(grant)
            restriction = self._invocation_restriction(
                assignment,
                now=utc_now(),
            )
            if (
                restriction is None
                and outbox.kind == "work_dispatch"
                and not await self._dependencies_accepted(work_item)
            ):
                restriction = "work item dependencies are not accepted"
            handler = self.handlers.get(outbox.destination_role)
            handler_invoked = False
            invocation_fence_id: UUID | None = None
            if restriction is not None:
                outcome = DispatchOutcome(
                    status=DispatchOutcomeStatus.reconciliation_required,
                    detail=f"agent handler was not invoked: {restriction}",
                )
            elif handler is None:
                outcome = DispatchOutcome(
                    status=DispatchOutcomeStatus.retry,
                    detail="agent runtime is unavailable",
                    retry_after_seconds=30,
                )
            else:
                fence = await self.store.acquire_dispatch_invocation_fence(
                    assignment_id=outbox.assignment_id,
                    outbox_id=outbox.outbox_id,
                    attempt=attempt,
                    claim_id=claim.claim_id,
                )
                invocation_fence_id = fence.fence.fence_id
                if not fence.acquired:
                    outcome = DispatchOutcome(
                        status=DispatchOutcomeStatus.reconciliation_required,
                        detail=(
                            "agent handler was not invoked: this outbox attempt "
                            "already has a durable invocation fence, so its prior "
                            "side-effect outcome requires reconciliation"
                        ),
                    )
                else:
                    handler_invoked = True
                    outcome = await self._invoke_with_heartbeat(
                        handler,
                        claim_id=claim.claim_id,
                        outbox=outbox,
                        assignment=assignment,
                        work_item=work_item,
                        grant=grant,
                    )
            final_assignment = await self.store.get_assignment(
                outbox.assignment_id
            )
            final_restriction = self._invocation_restriction(
                final_assignment,
                now=utc_now(),
            )
            if final_restriction is not None:
                assignment = final_assignment
                outcome = DispatchOutcome(
                    status=DispatchOutcomeStatus.reconciliation_required,
                    detail=(
                        "dispatch completion was fenced before finalization: "
                        f"{final_restriction}"
                    ),
                )
            elif handler_invoked:
                try:
                    await self.store.renew_outbox_claim(
                        outbox.outbox_id,
                        claim_id=claim.claim_id,
                        extend_seconds=self.claim_ttl_seconds,
                    )
                except CollaborativeDevelopmentConflict:
                    final_assignment = await self.store.get_assignment(
                        outbox.assignment_id
                    )
                    final_restriction = self._invocation_restriction(
                        final_assignment,
                        now=utc_now(),
                    )
                    if final_restriction is None:
                        raise
                    assignment = final_assignment
                    outcome = DispatchOutcome(
                        status=DispatchOutcomeStatus.reconciliation_required,
                        detail=(
                            "dispatch completion was fenced before finalization: "
                            f"{final_restriction}"
                        ),
                    )
            now = utc_now()
            authorization_request: DevelopmentAuthorizationRequest | None = None
            if outcome.status == DispatchOutcomeStatus.delivered:
                if outcome.requested_authority is not None:
                    raise RuntimeError("delivered dispatch cannot request more authority")
            elif outcome.status == DispatchOutcomeStatus.retry:
                if outcome.retry_after_seconds is None:
                    raise RuntimeError("retry dispatch requires retry_after_seconds")
            elif outcome.status == DispatchOutcomeStatus.authorization_required:
                if outcome.requested_authority is None:
                    raise RuntimeError(
                        "authorization_required dispatch requires requested authority"
                    )
                self._validate_authority_request(
                    grant,
                    assignment.budget,
                    outcome.requested_authority,
                )
                authorization_request = DevelopmentAuthorizationRequest(
                    request_id=uuid5(
                        _DISPATCH_NAMESPACE,
                        f"authorization:{outbox.outbox_id}",
                    ),
                    assignment_id=outbox.assignment_id,
                    work_item_id=outbox.work_item_id,
                    outbox_id=outbox.outbox_id,
                    destination_role=outbox.destination_role,
                    existing_grant_digest=grant_digest,
                    requested_authority=outcome.requested_authority,
                    created_at=now,
                )
            elif outcome.status != DispatchOutcomeStatus.reconciliation_required:
                raise RuntimeError("unsupported dispatch outcome")
            history = self._history(
                outbox=outbox,
                attempt=attempt,
                assignment=assignment,
                grant_digest=grant_digest,
                outcome=outcome,
                authorization_request=authorization_request,
                now=now,
                invocation_fence_id=invocation_fence_id,
            )
            persisted = await asyncio.to_thread(
                self.journal.record,
                history,
                authorization_request,
            )
            await self._finalize(persisted, claim_id=claim.claim_id)
            records.append(persisted)
        return records


__all__ = [
    "CollaborativeDevelopmentDispatchJournal",
    "CollaborativeDevelopmentDispatcher",
    "DevelopmentAuthorizationRequest",
    "DevelopmentDispatchHandler",
    "DispatchHistoryRecord",
    "DispatchOutcome",
    "DispatchOutcomeStatus",
    "RequestedAuthority",
    "RoleBoundDispatchContext",
    "canonical_digest",
]
