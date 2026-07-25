"""Durable user decisions for collaborative-development authority requests.

The autonomous dispatcher owns the request journal.  This adapter first
persists the exact user decision and requested replacement.  The owner-only
service can then CAS-apply that approved grant revision in the assignment store
and mark this record applied.  A crash between those steps is safely resumable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .collaborative_development_dispatcher import (
    DevelopmentAuthorizationRequest,
    RequestedAuthority,
)
from .collaborative_development_models import (
    AgentRole,
    DevelopmentBudget,
    WorkspaceGrant,
    utc_now,
)


class DevelopmentAuthorityDecisionError(RuntimeError):
    """Base error for the authority-decision journal."""


class DevelopmentAuthorityRequestNotFound(DevelopmentAuthorityDecisionError):
    """The requested durable authority request does not exist."""


class DevelopmentAuthorityDecisionConflict(DevelopmentAuthorityDecisionError):
    """A decision or idempotency key conflicts with durable state."""


class AuthorityDecision(str, Enum):
    approved = "approved"
    rejected = "rejected"


class DevelopmentAuthorizationRequestView(BaseModel):
    """User-safe request projection with a separately persisted decision."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    assignment_id: UUID
    work_item_id: UUID
    outbox_id: UUID
    destination_role: AgentRole
    existing_grant_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_authority: RequestedAuthority
    status: Literal["pending", "approved", "rejected"]
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None
    replacement_grant: WorkspaceGrant | None = None
    replacement_budget: DevelopmentBudget | None = None
    expected_assignment_revision: int | None = Field(default=None, ge=1)
    applied_at: datetime | None = None
    grant_changed: bool = False
    execution_resumed: bool = False
    next_action: Literal[
        "await_user_decision",
        "retry_approved_grant_revision",
        "continue_dispatch",
        "revise_or_close_work_item",
    ]


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class CollaborativeDevelopmentAuthorityStore:
    """Read dispatcher requests and append user decisions without grant changes."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _prepare_private_database(self) -> None:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.database_path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not metadata.st_mode & 0o170000 == 0o100000:
                raise DevelopmentAuthorityDecisionError(
                    "authority decision database must be a regular file"
                )
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def _tighten_database_files(self) -> None:
        for path in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            try:
                path.chmod(0o600)
            except FileNotFoundError:
                continue
            except OSError as error:
                raise DevelopmentAuthorityDecisionError(
                    "authority decision database permissions could not be secured"
                ) from error

    def _initialize_sync(self) -> None:
        self._prepare_private_database()
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS development_authorization_requests (
                  request_id TEXT PRIMARY KEY,
                  outbox_id TEXT NOT NULL UNIQUE,
                  assignment_id TEXT NOT NULL,
                  work_item_id TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS development_authorization_request_decisions (
                  request_id TEXT PRIMARY KEY,
                  assignment_id TEXT NOT NULL,
                  decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
                  actor_id TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  decided_at TEXT NOT NULL,
                  replacement_grant_json TEXT,
                  replacement_budget_json TEXT,
                  expected_assignment_revision INTEGER,
                  applied_at TEXT,
                  UNIQUE(actor_id,idempotency_key),
                  FOREIGN KEY(request_id)
                    REFERENCES development_authorization_requests(request_id)
                );
                """
            )
            decision_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(development_authorization_request_decisions)"
                ).fetchall()
            }
            migrations = {
                "replacement_grant_json": (
                    "ALTER TABLE development_authorization_request_decisions "
                    "ADD COLUMN replacement_grant_json TEXT"
                ),
                "replacement_budget_json": (
                    "ALTER TABLE development_authorization_request_decisions "
                    "ADD COLUMN replacement_budget_json TEXT"
                ),
                "expected_assignment_revision": (
                    "ALTER TABLE development_authorization_request_decisions "
                    "ADD COLUMN expected_assignment_revision INTEGER"
                ),
                "applied_at": (
                    "ALTER TABLE development_authorization_request_decisions "
                    "ADD COLUMN applied_at TEXT"
                ),
            }
            for column, statement in migrations.items():
                if column not in decision_columns:
                    connection.execute(statement)
            connection.commit()
        except sqlite3.Error as error:
            raise DevelopmentAuthorityDecisionError(
                "authority decision journal initialization failed"
            ) from error
        finally:
            connection.close()
            self._tighten_database_files()

    @staticmethod
    def _view(
        request: DevelopmentAuthorizationRequest,
        decision_row: sqlite3.Row | None,
    ) -> DevelopmentAuthorizationRequestView:
        payload = request.model_dump(mode="python", exclude={"status"})
        if decision_row is None:
            return DevelopmentAuthorizationRequestView(
                **payload,
                status="pending",
                next_action="await_user_decision",
            )
        decision = AuthorityDecision(str(decision_row["decision"]))
        replacement_grant = (
            WorkspaceGrant.model_validate_json(
                str(decision_row["replacement_grant_json"])
            )
            if decision_row["replacement_grant_json"] is not None
            else None
        )
        replacement_budget = (
            DevelopmentBudget.model_validate_json(
                str(decision_row["replacement_budget_json"])
            )
            if decision_row["replacement_budget_json"] is not None
            else None
        )
        applied_at = (
            datetime.fromisoformat(str(decision_row["applied_at"]))
            if decision_row["applied_at"] is not None
            else None
        )
        return DevelopmentAuthorizationRequestView(
            **payload,
            status=decision.value,
            decided_at=datetime.fromisoformat(str(decision_row["decided_at"])),
            decided_by=str(decision_row["actor_id"]),
            decision_reason=str(decision_row["reason"]),
            replacement_grant=replacement_grant,
            replacement_budget=replacement_budget,
            expected_assignment_revision=decision_row[
                "expected_assignment_revision"
            ],
            applied_at=applied_at,
            grant_changed=applied_at is not None,
            execution_resumed=applied_at is not None,
            next_action=(
                (
                    "continue_dispatch"
                    if applied_at is not None
                    else "retry_approved_grant_revision"
                )
                if decision == AuthorityDecision.approved
                else "revise_or_close_work_item"
            ),
        )

    async def list_requests(
        self,
        assignment_id: UUID,
        *,
        status: Literal["pending", "approved", "rejected", "all"] = "pending",
    ) -> list[DevelopmentAuthorizationRequestView]:
        return await asyncio.to_thread(
            self._list_requests_sync,
            assignment_id,
            status,
        )

    async def get_request(
        self,
        assignment_id: UUID,
        request_id: UUID,
    ) -> DevelopmentAuthorizationRequestView:
        requests = await self.list_requests(assignment_id, status="all")
        for request in requests:
            if request.request_id == request_id:
                return request
        raise DevelopmentAuthorityRequestNotFound(
            "authority request was not found"
        )

    def _list_requests_sync(
        self,
        assignment_id: UUID,
        status: Literal["pending", "approved", "rejected", "all"],
    ) -> list[DevelopmentAuthorizationRequestView]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT request.payload_json,
                       decision.decision,
                       decision.actor_id,
                       decision.reason,
                       decision.decided_at,
                       decision.replacement_grant_json,
                       decision.replacement_budget_json,
                       decision.expected_assignment_revision,
                       decision.applied_at
                FROM development_authorization_requests AS request
                LEFT JOIN development_authorization_request_decisions AS decision
                  ON decision.request_id=request.request_id
                WHERE request.assignment_id=?
                ORDER BY request.created_at,request.request_id
                """,
                (str(assignment_id),),
            ).fetchall()
            views = [
                self._view(
                    DevelopmentAuthorizationRequest.model_validate_json(
                        str(row["payload_json"])
                    ),
                    row if row["decision"] is not None else None,
                )
                for row in rows
            ]
            if status == "all":
                return views
            return [view for view in views if view.status == status]
        except (sqlite3.Error, ValueError) as error:
            raise DevelopmentAuthorityDecisionError(
                "authority requests could not be read"
            ) from error
        finally:
            connection.close()
            self._tighten_database_files()

    async def decide(
        self,
        *,
        assignment_id: UUID,
        request_id: UUID,
        decision: AuthorityDecision,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        replacement_grant: WorkspaceGrant | None = None,
        replacement_budget: DevelopmentBudget | None = None,
        expected_assignment_revision: int | None = None,
    ) -> DevelopmentAuthorizationRequestView:
        return await asyncio.to_thread(
            self._decide_sync,
            assignment_id,
            request_id,
            decision,
            actor_id,
            reason,
            idempotency_key,
            replacement_grant,
            replacement_budget,
            expected_assignment_revision,
        )

    def _decide_sync(
        self,
        assignment_id: UUID,
        request_id: UUID,
        decision: AuthorityDecision,
        actor_id: str,
        reason: str,
        idempotency_key: str,
        replacement_grant: WorkspaceGrant | None,
        replacement_budget: DevelopmentBudget | None,
        expected_assignment_revision: int | None,
    ) -> DevelopmentAuthorizationRequestView:
        if decision == AuthorityDecision.approved:
            if replacement_grant is None or expected_assignment_revision is None:
                raise ValueError(
                    "approval requires a replacement grant and assignment revision"
                )
        elif (
            replacement_grant is not None
            or replacement_budget is not None
            or expected_assignment_revision is not None
        ):
            raise ValueError("rejection cannot carry a replacement authority grant")
        request_payload = {
            "assignment_id": str(assignment_id),
            "request_id": str(request_id),
            "decision": decision.value,
            "reason": reason,
            "replacement_grant": (
                replacement_grant.model_dump(mode="json")
                if replacement_grant is not None
                else None
            ),
            "replacement_budget": (
                replacement_budget.model_dump(mode="json")
                if replacement_budget is not None
                else None
            ),
            "expected_assignment_revision": expected_assignment_revision,
        }
        request_digest = _canonical_digest(request_payload)
        decided_at = utc_now()
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            request_row = connection.execute(
                """
                SELECT payload_json FROM development_authorization_requests
                WHERE request_id=? AND assignment_id=?
                """,
                (str(request_id), str(assignment_id)),
            ).fetchone()
            if request_row is None:
                raise DevelopmentAuthorityRequestNotFound(
                    "authority request was not found"
                )
            request = DevelopmentAuthorizationRequest.model_validate_json(
                str(request_row["payload_json"])
            )

            replay = connection.execute(
                """
                SELECT * FROM development_authorization_request_decisions
                WHERE actor_id=? AND idempotency_key=?
                """,
                (actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if str(replay["request_digest"]) != request_digest:
                    raise DevelopmentAuthorityDecisionConflict(
                        "idempotency key was reused with a different decision"
                    )
                connection.rollback()
                return self._view(request, replay)

            existing = connection.execute(
                """
                SELECT * FROM development_authorization_request_decisions
                WHERE request_id=?
                """,
                (str(request_id),),
            ).fetchone()
            if existing is not None:
                raise DevelopmentAuthorityDecisionConflict(
                    "authority request already has a durable decision"
                )
            connection.execute(
                """
                INSERT INTO development_authorization_request_decisions(
                  request_id,assignment_id,decision,actor_id,reason,
                  idempotency_key,request_digest,decided_at,
                  replacement_grant_json,replacement_budget_json,
                  expected_assignment_revision,applied_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)
                """,
                (
                    str(request_id),
                    str(assignment_id),
                    decision.value,
                    actor_id,
                    reason,
                    idempotency_key,
                    request_digest,
                    decided_at.isoformat(),
                    (
                        replacement_grant.model_dump_json()
                        if replacement_grant is not None
                        else None
                    ),
                    (
                        replacement_budget.model_dump_json()
                        if replacement_budget is not None
                        else None
                    ),
                    expected_assignment_revision,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM development_authorization_request_decisions
                WHERE request_id=?
                """,
                (str(request_id),),
            ).fetchone()
            connection.commit()
            if row is None:  # pragma: no cover - guarded by the insert above
                raise DevelopmentAuthorityDecisionError(
                    "authority decision could not be reloaded"
                )
            return self._view(request, row)
        except (
            DevelopmentAuthorityDecisionConflict,
            DevelopmentAuthorityRequestNotFound,
        ):
            connection.rollback()
            raise
        except (sqlite3.Error, ValueError) as error:
            connection.rollback()
            raise DevelopmentAuthorityDecisionError(
                "authority decision could not be persisted"
            ) from error
        finally:
            connection.close()
            self._tighten_database_files()

    async def mark_applied(
        self,
        *,
        assignment_id: UUID,
        request_id: UUID,
    ) -> DevelopmentAuthorizationRequestView:
        return await asyncio.to_thread(
            self._mark_applied_sync,
            assignment_id,
            request_id,
        )

    def _mark_applied_sync(
        self,
        assignment_id: UUID,
        request_id: UUID,
    ) -> DevelopmentAuthorizationRequestView:
        applied_at = utc_now()
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            request_row = connection.execute(
                """
                SELECT payload_json FROM development_authorization_requests
                WHERE request_id=? AND assignment_id=?
                """,
                (str(request_id), str(assignment_id)),
            ).fetchone()
            decision_row = connection.execute(
                """
                SELECT * FROM development_authorization_request_decisions
                WHERE request_id=? AND assignment_id=? AND decision='approved'
                """,
                (str(request_id), str(assignment_id)),
            ).fetchone()
            if request_row is None or decision_row is None:
                raise DevelopmentAuthorityRequestNotFound(
                    "approved authority request was not found"
                )
            if decision_row["applied_at"] is None:
                connection.execute(
                    """
                    UPDATE development_authorization_request_decisions
                    SET applied_at=? WHERE request_id=?
                    """,
                    (applied_at.isoformat(), str(request_id)),
                )
                decision_row = connection.execute(
                    """
                    SELECT * FROM development_authorization_request_decisions
                    WHERE request_id=?
                    """,
                    (str(request_id),),
                ).fetchone()
            connection.commit()
            if decision_row is None:  # pragma: no cover
                raise DevelopmentAuthorityDecisionError(
                    "applied authority decision could not be reloaded"
                )
            request = DevelopmentAuthorizationRequest.model_validate_json(
                str(request_row["payload_json"])
            )
            return self._view(request, decision_row)
        except (
            DevelopmentAuthorityDecisionError,
            DevelopmentAuthorityRequestNotFound,
        ):
            connection.rollback()
            raise
        except (sqlite3.Error, ValueError) as error:
            connection.rollback()
            raise DevelopmentAuthorityDecisionError(
                "authority application could not be persisted"
            ) from error
        finally:
            connection.close()
            self._tighten_database_files()


__all__ = [
    "AuthorityDecision",
    "CollaborativeDevelopmentAuthorityStore",
    "DevelopmentAuthorityDecisionConflict",
    "DevelopmentAuthorityDecisionError",
    "DevelopmentAuthorityRequestNotFound",
    "DevelopmentAuthorizationRequestView",
]
