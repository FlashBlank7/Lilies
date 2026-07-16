from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .models import utc_now
from .storage import Storage


DurableJobStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "paused",
    "succeeded",
    "failed",
    "cancelled",
]
DurableTriggerKind = Literal["schedule", "manual", "event"]
CollectionReceiptStatus = Literal[
    "new",
    "changed",
    "unchanged",
    "denied",
    "oversized",
    "failed",
]


class DurableJobRecord(BaseModel):
    id: str
    idempotency_key: str
    application_id: str
    version: int
    node_id: str
    trigger_kind: DurableTriggerKind
    local_date: str = ""
    status: DurableJobStatus = "queued"
    payload: dict[str, Any] = Field(default_factory=dict)
    attempt_count: int = 0
    max_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    next_attempt_at: str
    lease_owner: str | None = None
    lease_expires_at: str | None = None
    lease_version: int = 0
    run_id: str | None = None
    platform_task_id: str | None = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    alert: dict[str, Any] | None = None
    cancel_requested: bool = False
    revision: int = 1
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class DurableJobEvent(BaseModel):
    sequence: int
    job_id: str
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class DurableJobAttempt(BaseModel):
    job_id: str
    attempt_number: int
    status: Literal["running", "paused", "succeeded", "failed", "cancelled"]
    worker_id: str
    lease_version: int
    platform_task_id: str
    run_id: str | None = None
    error: str = ""
    started_at: str
    finished_at: str | None = None


class CollectionReceipt(BaseModel):
    id: str
    job_id: str
    application_id: str
    run_id: str
    source_key: str
    requested_url: str
    final_url: str
    canonical_url: str
    host: str
    permission_basis: str
    robots_checked: bool
    robots_allowed: bool | None = None
    status: CollectionReceiptStatus
    http_status: int | None = None
    content_type: str = ""
    content_bytes: int = 0
    content_hash: str = ""
    previous_receipt_id: str | None = None
    previous_content_hash: str = ""
    title: str = ""
    excerpt: str = ""
    transformation: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    collected_at: str
    created_at: str
    updated_at: str


class DurableJobConflict(RuntimeError):
    pass


class DurableJobStore:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self.storage._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS durable_jobs (
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  application_id TEXT NOT NULL,
                  version INTEGER NOT NULL,
                  node_id TEXT NOT NULL,
                  trigger_kind TEXT NOT NULL,
                  local_date TEXT NOT NULL,
                  status TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  attempt_count INTEGER NOT NULL,
                  max_attempts INTEGER NOT NULL,
                  retry_backoff_seconds REAL NOT NULL,
                  next_attempt_at TEXT NOT NULL,
                  lease_owner TEXT,
                  lease_expires_at TEXT,
                  lease_version INTEGER NOT NULL,
                  run_id TEXT,
                  platform_task_id TEXT,
                  checkpoint_json TEXT NOT NULL,
                  result_json TEXT NOT NULL,
                  error TEXT NOT NULL,
                  alert_json TEXT,
                  cancel_requested INTEGER NOT NULL,
                  revision INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_durable_jobs_app_created
                  ON durable_jobs(application_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_durable_jobs_claim
                  ON durable_jobs(status, next_attempt_at, created_at);
                CREATE TABLE IF NOT EXISTS durable_job_events (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                  job_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(job_id) REFERENCES durable_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_durable_job_events_job
                  ON durable_job_events(job_id, sequence);
                CREATE TABLE IF NOT EXISTS durable_job_attempts (
                  job_id TEXT NOT NULL,
                  attempt_number INTEGER NOT NULL,
                  status TEXT NOT NULL,
                  worker_id TEXT NOT NULL,
                  lease_version INTEGER NOT NULL,
                  platform_task_id TEXT NOT NULL,
                  run_id TEXT,
                  error TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  finished_at TEXT,
                  PRIMARY KEY(job_id, attempt_number),
                  FOREIGN KEY(job_id) REFERENCES durable_jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS collection_receipts (
                  id TEXT PRIMARY KEY,
                  job_id TEXT NOT NULL,
                  application_id TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  source_key TEXT NOT NULL,
                  requested_url TEXT NOT NULL,
                  final_url TEXT NOT NULL,
                  canonical_url TEXT NOT NULL,
                  host TEXT NOT NULL,
                  permission_basis TEXT NOT NULL,
                  robots_checked INTEGER NOT NULL,
                  robots_allowed INTEGER,
                  status TEXT NOT NULL,
                  http_status INTEGER,
                  content_type TEXT NOT NULL,
                  content_bytes INTEGER NOT NULL,
                  content_hash TEXT NOT NULL,
                  previous_receipt_id TEXT,
                  previous_content_hash TEXT NOT NULL,
                  title TEXT NOT NULL,
                  excerpt TEXT NOT NULL,
                  transformation_json TEXT NOT NULL,
                  error TEXT NOT NULL,
                  collected_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(job_id, source_key),
                  FOREIGN KEY(job_id) REFERENCES durable_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_collection_receipts_job
                  ON collection_receipts(job_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_collection_receipts_canonical
                  ON collection_receipts(application_id, canonical_url, created_at DESC);
                """
            )

    async def enqueue(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        application_id: str,
        version: int,
        node_id: str,
        trigger_kind: DurableTriggerKind,
        local_date: str,
        payload: dict[str, Any],
        max_attempts: int,
        retry_backoff_seconds: float,
        available_at: datetime | None = None,
    ) -> DurableJobRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._enqueue_sync,
                job_id,
                idempotency_key,
                application_id,
                version,
                node_id,
                trigger_kind,
                local_date,
                payload,
                max_attempts,
                retry_backoff_seconds,
                available_at or datetime.now(timezone.utc),
            )

    def _enqueue_sync(
        self,
        job_id: str,
        idempotency_key: str,
        application_id: str,
        version: int,
        node_id: str,
        trigger_kind: DurableTriggerKind,
        local_date: str,
        payload: dict[str, Any],
        max_attempts: int,
        retry_backoff_seconds: float,
        available_at: datetime,
    ) -> DurableJobRecord:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM durable_jobs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if existing:
                record = self._job_from_row(dict(existing))
                immutable = (
                    record.application_id,
                    record.version,
                    record.node_id,
                    record.trigger_kind,
                    record.local_date,
                    self._idempotency_payload(record.payload),
                    record.max_attempts,
                    record.retry_backoff_seconds,
                )
                if immutable != (
                    application_id,
                    version,
                    node_id,
                    trigger_kind,
                    local_date,
                    self._idempotency_payload(payload),
                    max(1, max_attempts),
                    max(0.0, retry_backoff_seconds),
                ):
                    raise DurableJobConflict("idempotency key is already bound to another job")
                return record
            conn.execute(
                """INSERT INTO durable_jobs
                   (id,idempotency_key,application_id,version,node_id,trigger_kind,local_date,status,
                    payload_json,attempt_count,max_attempts,retry_backoff_seconds,next_attempt_at,
                    lease_owner,lease_expires_at,lease_version,run_id,platform_task_id,checkpoint_json,
                    result_json,error,alert_json,cancel_requested,revision,created_at,updated_at,
                    started_at,finished_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    idempotency_key,
                    application_id,
                    version,
                    node_id,
                    trigger_kind,
                    local_date,
                    "queued",
                    json.dumps(payload),
                    0,
                    max(1, max_attempts),
                    max(0.0, retry_backoff_seconds),
                    available_at.astimezone(timezone.utc).isoformat(),
                    None,
                    None,
                    0,
                    None,
                    None,
                    "{}",
                    "{}",
                    "",
                    None,
                    0,
                    1,
                    now,
                    now,
                    None,
                    None,
                ),
            )
            self._append_event_sync(
                conn,
                job_id,
                "job.enqueued",
                {"idempotency_key": idempotency_key, "trigger_kind": trigger_kind},
                now,
            )
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_from_row(dict(row))

    @staticmethod
    def _idempotency_payload(payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized.pop("triggered_at", None)
        return normalized

    async def get(self, job_id: str) -> DurableJobRecord:
        row = await asyncio.to_thread(
            self.storage._get_one, "SELECT * FROM durable_jobs WHERE id=?", (job_id,)
        )
        return self._job_from_row(row)

    async def list(
        self,
        application_id: str | None = None,
        *,
        statuses: set[DurableJobStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[DurableJobRecord]:
        return await asyncio.to_thread(
            self._list_sync,
            application_id,
            statuses or set(),
            max(1, min(limit, 200)),
            max(0, offset),
        )

    def _list_sync(
        self,
        application_id: str | None,
        statuses: set[DurableJobStatus],
        limit: int,
        offset: int,
    ) -> list[DurableJobRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        if application_id:
            clauses.append("application_id=?")
            values.append(application_id)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            values.extend(sorted(statuses))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.extend((limit, offset))
        with self.storage._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM durable_jobs {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                values,
            ).fetchall()
            return [self._job_from_row(dict(row)) for row in rows]

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_seconds: float,
        now: datetime | None = None,
        application_id: str | None = None,
    ) -> DurableJobRecord | None:
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_next_sync,
                worker_id,
                max(0.001, lease_seconds),
                now or datetime.now(timezone.utc),
                application_id,
            )

    def _claim_next_sync(
        self,
        worker_id: str,
        lease_seconds: float,
        now: datetime,
        application_id: str | None,
    ) -> DurableJobRecord | None:
        now_iso = now.astimezone(timezone.utc).isoformat()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            application_clause = "AND application_id=?" if application_id else ""
            values: tuple[Any, ...] = (now_iso, application_id) if application_id else (now_iso,)
            row = conn.execute(
                f"""SELECT * FROM durable_jobs
                    WHERE status IN ('queued','retry_wait') AND next_attempt_at<=?
                      AND cancel_requested=0 {application_clause}
                    ORDER BY next_attempt_at, created_at LIMIT 1""",
                values,
            ).fetchone()
            if not row:
                return None
            record = self._job_from_row(dict(row))
            requested_lease = max(
                float(record.payload.get("lease_seconds", lease_seconds)),
                0.001,
            )
            expires = (
                now.astimezone(timezone.utc) + timedelta(seconds=requested_lease)
            ).isoformat()
            attempt = record.attempt_count + 1
            lease_version = record.lease_version + 1
            task_id = f"{record.id}:attempt:{attempt}"
            revision = record.revision + 1
            conn.execute(
                """UPDATE durable_jobs SET status='running',attempt_count=?,lease_owner=?,
                   lease_expires_at=?,lease_version=?,run_id=NULL,platform_task_id=?,error='',
                   alert_json=NULL,revision=?,updated_at=?,started_at=COALESCE(started_at,?),finished_at=NULL
                   WHERE id=? AND revision=?""",
                (
                    attempt,
                    worker_id,
                    expires,
                    lease_version,
                    task_id,
                    revision,
                    now_iso,
                    now_iso,
                    record.id,
                    record.revision,
                ),
            )
            conn.execute(
                """INSERT INTO durable_job_attempts
                   (job_id,attempt_number,status,worker_id,lease_version,platform_task_id,run_id,
                    error,started_at,finished_at)
                   VALUES(?,?,?,?,?,?,NULL,'',?,NULL)""",
                (record.id, attempt, "running", worker_id, lease_version, task_id, now_iso),
            )
            self._append_event_sync(
                conn,
                record.id,
                "job.claimed",
                {
                    "attempt": attempt,
                    "worker_id": worker_id,
                    "lease_version": lease_version,
                    "lease_expires_at": expires,
                },
                now_iso,
            )
            updated = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (record.id,)).fetchone()
            return self._job_from_row(dict(updated))

    async def renew(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        lease_seconds: float,
    ) -> DurableJobRecord:
        return await self._fenced_update(
            job_id,
            worker_id=worker_id,
            lease_version=lease_version,
            event_type="job.lease_renewed",
            updates={
                "lease_expires_at": (
                    datetime.now(timezone.utc) + timedelta(seconds=max(0.001, lease_seconds))
                ).isoformat()
            },
        )

    async def attach_run(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        run_id: str,
    ) -> DurableJobRecord:
        record = await self._fenced_update(
            job_id,
            worker_id=worker_id,
            lease_version=lease_version,
            event_type="job.run_attached",
            updates={"run_id": run_id},
            event_data={"run_id": run_id},
        )
        await asyncio.to_thread(self._update_attempt_run_sync, record, run_id)
        return record

    def _update_attempt_run_sync(self, record: DurableJobRecord, run_id: str) -> None:
        self.storage._execute(
            "UPDATE durable_job_attempts SET run_id=? WHERE job_id=? AND attempt_number=?",
            (run_id, record.id, record.attempt_count),
        )

    async def checkpoint(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        values: dict[str, Any],
    ) -> DurableJobRecord:
        current = await self.get(job_id)
        checkpoint = {**current.checkpoint, **values}
        return await self._fenced_update(
            job_id,
            worker_id=worker_id,
            lease_version=lease_version,
            event_type="job.checkpointed",
            updates={"checkpoint_json": json.dumps(checkpoint)},
            event_data={"keys": sorted(values)},
        )

    async def mark_paused(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        reason: str,
    ) -> DurableJobRecord:
        record = await self._fenced_update(
            job_id,
            worker_id=worker_id,
            lease_version=lease_version,
            event_type="job.paused",
            updates={"status": "paused", "lease_owner": None, "lease_expires_at": None},
            event_data={"reason": reason},
        )
        await asyncio.to_thread(self._finish_attempt_sync, record, "paused", reason)
        return record

    async def complete(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        result: dict[str, Any],
    ) -> DurableJobRecord:
        now = utc_now()
        record = await self._fenced_update(
            job_id,
            worker_id=worker_id,
            lease_version=lease_version,
            event_type="job.succeeded",
            updates={
                "status": "succeeded",
                "result_json": json.dumps(result),
                "lease_owner": None,
                "lease_expires_at": None,
                "finished_at": now,
            },
            event_data={"run_id": result.get("run_id")},
        )
        await asyncio.to_thread(self._finish_attempt_sync, record, "succeeded", "")
        return record

    async def fail(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        error: str,
        retryable: bool,
    ) -> DurableJobRecord:
        current = await self.get(job_id)
        self._assert_fence(current, worker_id, lease_version)
        now = datetime.now(timezone.utc)
        should_retry = retryable and current.attempt_count < current.max_attempts
        if should_retry:
            delay = current.retry_backoff_seconds * (2 ** max(current.attempt_count - 1, 0))
            next_attempt_at = (now + timedelta(seconds=delay)).isoformat()
            status: DurableJobStatus = "retry_wait"
            alert = None
            event_type = "job.retry_scheduled"
        else:
            next_attempt_at = current.next_attempt_at
            status = "failed"
            alert = {
                "code": "durable_job_failed",
                "severity": "error",
                "message": error,
                "attempts": current.attempt_count,
                "created_at": now.isoformat(),
            }
            event_type = "job.failed"
        record = await self._fenced_update(
            job_id,
            worker_id=worker_id,
            lease_version=lease_version,
            event_type=event_type,
            updates={
                "status": status,
                "next_attempt_at": next_attempt_at,
                "run_id": None if should_retry else current.run_id,
                "lease_owner": None,
                "lease_expires_at": None,
                "error": error,
                "alert_json": json.dumps(alert) if alert else None,
                "finished_at": None if should_retry else now.isoformat(),
            },
            event_data={
                "error": error,
                "retryable": retryable,
                "next_attempt_at": next_attempt_at if should_retry else None,
            },
        )
        await asyncio.to_thread(self._finish_attempt_sync, current, "failed", error)
        return record

    async def reconcile_run(
        self,
        job_id: str,
        *,
        run_id: str,
        run_status: str,
        outputs: dict[str, Any] | None = None,
        error: str = "",
    ) -> DurableJobRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._reconcile_run_sync,
                job_id,
                run_id,
                run_status,
                outputs or {},
                error,
            )

    def _reconcile_run_sync(
        self,
        job_id: str,
        run_id: str,
        run_status: str,
        outputs: dict[str, Any],
        error: str,
    ) -> DurableJobRecord:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            record = self._job_from_row(dict(row))
            if record.status != "running" or record.run_id != run_id:
                raise DurableJobConflict(
                    "durable job run reconciliation rejected stale run identity"
                )
            if run_status == "succeeded":
                next_status: DurableJobStatus = "succeeded"
                next_attempt_at = record.next_attempt_at
                result = {
                    **outputs,
                    "run_id": run_id,
                    "outputs": outputs,
                }
                alert = None
                finished_at = now_iso
                event_type = "job.succeeded"
                attempt_status = "succeeded"
                attempt_error = ""
            elif run_status == "paused":
                next_status = "paused"
                next_attempt_at = record.next_attempt_at
                result = record.result
                alert = None
                finished_at = None
                event_type = "job.paused"
                attempt_status = "paused"
                attempt_error = error
            elif run_status == "cancelled":
                next_status = "cancelled"
                next_attempt_at = record.next_attempt_at
                result = record.result
                alert = None
                finished_at = now_iso
                event_type = "job.cancelled"
                attempt_status = "cancelled"
                attempt_error = error or "workflow run cancelled"
            elif run_status == "failed":
                should_retry = record.attempt_count < record.max_attempts
                next_status = "retry_wait" if should_retry else "failed"
                delay = record.retry_backoff_seconds * (
                    2 ** max(record.attempt_count - 1, 0)
                )
                next_attempt_at = (
                    (now + timedelta(seconds=delay)).isoformat()
                    if should_retry
                    else record.next_attempt_at
                )
                result = record.result
                alert = (
                    None
                    if should_retry
                    else {
                        "code": "durable_job_failed",
                        "severity": "error",
                        "message": error or "workflow run failed",
                        "attempts": record.attempt_count,
                        "created_at": now_iso,
                    }
                )
                finished_at = None if should_retry else now_iso
                event_type = "job.retry_scheduled" if should_retry else "job.failed"
                attempt_status = "failed"
                attempt_error = error or "workflow run failed"
            else:
                raise DurableJobConflict(
                    f"workflow run is not terminal or paused: {run_status}"
                )
            revision = record.revision + 1
            next_lease_version = record.lease_version + 1
            conn.execute(
                """UPDATE durable_jobs SET status=?,next_attempt_at=?,lease_owner=NULL,
                   lease_expires_at=NULL,lease_version=?,run_id=?,result_json=?,error=?,
                   alert_json=?,revision=?,updated_at=?,finished_at=? WHERE id=? AND revision=?""",
                (
                    next_status,
                    next_attempt_at,
                    next_lease_version,
                    None if next_status == "retry_wait" else run_id,
                    json.dumps(result),
                    attempt_error,
                    json.dumps(alert) if alert else None,
                    revision,
                    now_iso,
                    finished_at,
                    job_id,
                    record.revision,
                ),
            )
            conn.execute(
                """UPDATE durable_job_attempts SET status=?,error=?,finished_at=?
                   WHERE job_id=? AND attempt_number=?""",
                (
                    attempt_status,
                    attempt_error,
                    now_iso,
                    job_id,
                    record.attempt_count,
                ),
            )
            self._append_event_sync(
                conn,
                job_id,
                event_type,
                {
                    "run_id": run_id,
                    "run_status": run_status,
                    "error": attempt_error,
                    "next_attempt_at": (
                        next_attempt_at if next_status == "retry_wait" else None
                    ),
                },
                now_iso,
            )
            updated = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_from_row(dict(updated))

    async def recover_expired(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        reason: str = "worker lease expired before terminal workflow evidence",
    ) -> DurableJobRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._recover_expired_sync,
                job_id,
                now or datetime.now(timezone.utc),
                reason,
            )

    def _recover_expired_sync(
        self,
        job_id: str,
        now: datetime,
        reason: str,
    ) -> DurableJobRecord:
        now_iso = now.astimezone(timezone.utc).isoformat()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            record = self._job_from_row(dict(row))
            if record.status != "running" or not record.lease_expires_at:
                raise DurableJobConflict("durable job has no expired running lease")
            if self._parse_time(record.lease_expires_at) > now.astimezone(timezone.utc):
                raise DurableJobConflict("durable job lease has not expired")
            should_retry = record.attempt_count < record.max_attempts
            next_status: DurableJobStatus = "retry_wait" if should_retry else "failed"
            delay = record.retry_backoff_seconds * (2 ** max(record.attempt_count - 1, 0))
            next_attempt_at = (
                (now.astimezone(timezone.utc) + timedelta(seconds=delay)).isoformat()
                if should_retry
                else record.next_attempt_at
            )
            alert = (
                None
                if should_retry
                else {
                    "code": "durable_job_lease_exhausted",
                    "severity": "error",
                    "message": reason,
                    "attempts": record.attempt_count,
                    "created_at": now_iso,
                }
            )
            conn.execute(
                """UPDATE durable_jobs SET status=?,next_attempt_at=?,lease_owner=NULL,
                   lease_expires_at=NULL,lease_version=?,run_id=?,error=?,alert_json=?,
                   revision=?,updated_at=?,finished_at=? WHERE id=? AND revision=?""",
                (
                    next_status,
                    next_attempt_at,
                    record.lease_version + 1,
                    None if should_retry else record.run_id,
                    reason,
                    json.dumps(alert) if alert else None,
                    record.revision + 1,
                    now_iso,
                    None if should_retry else now_iso,
                    job_id,
                    record.revision,
                ),
            )
            conn.execute(
                """UPDATE durable_job_attempts SET status='failed',error=?,finished_at=?
                   WHERE job_id=? AND attempt_number=?""",
                (reason, now_iso, job_id, record.attempt_count),
            )
            self._append_event_sync(
                conn,
                job_id,
                "job.lease_expired",
                {
                    "attempt": record.attempt_count,
                    "expired_lease_version": record.lease_version,
                    "retry_scheduled": should_retry,
                },
                now_iso,
            )
            if should_retry:
                self._append_event_sync(
                    conn,
                    job_id,
                    "job.retry_scheduled",
                    {"error": reason, "next_attempt_at": next_attempt_at},
                    now_iso,
                )
            updated = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_from_row(dict(updated))

    async def cancel_terminal(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        reason: str,
    ) -> DurableJobRecord:
        now = utc_now()
        record = await self._fenced_update(
            job_id,
            worker_id=worker_id,
            lease_version=lease_version,
            event_type="job.cancelled",
            updates={
                "status": "cancelled",
                "cancel_requested": 1,
                "lease_owner": None,
                "lease_expires_at": None,
                "error": reason,
                "finished_at": now,
            },
            event_data={"reason": reason},
        )
        await asyncio.to_thread(self._finish_attempt_sync, record, "cancelled", reason)
        return record

    async def request_cancel(self, job_id: str, *, expected_revision: int) -> DurableJobRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._request_cancel_sync, job_id, expected_revision
            )

    def _request_cancel_sync(self, job_id: str, expected_revision: int) -> DurableJobRecord:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            record = self._job_from_row(dict(row))
            if record.revision != expected_revision:
                raise DurableJobConflict(
                    f"durable job revision conflict: expected {expected_revision}, current {record.revision}"
                )
            if record.status in {"succeeded", "failed", "cancelled"}:
                raise DurableJobConflict(f"durable job cannot be cancelled from {record.status}")
            terminal = record.status in {"queued", "retry_wait", "paused"}
            next_status = "cancelled" if terminal else record.status
            revision = record.revision + 1
            conn.execute(
                """UPDATE durable_jobs SET cancel_requested=1,status=?,revision=?,updated_at=?,
                   finished_at=?,lease_owner=?,lease_expires_at=? WHERE id=? AND revision=?""",
                (
                    next_status,
                    revision,
                    now,
                    now if terminal else record.finished_at,
                    None if terminal else record.lease_owner,
                    None if terminal else record.lease_expires_at,
                    job_id,
                    record.revision,
                ),
            )
            self._append_event_sync(
                conn,
                job_id,
                "job.cancel_requested",
                {"terminal": terminal, "status": record.status},
                now,
            )
            updated = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_from_row(dict(updated))

    async def retry(self, job_id: str, *, expected_revision: int) -> DurableJobRecord:
        return await self._requeue(
            job_id,
            expected_revision=expected_revision,
            allowed={"failed", "cancelled"},
            event_type="job.retry_requested",
        )

    async def resume(self, job_id: str, *, expected_revision: int) -> DurableJobRecord:
        return await self._requeue(
            job_id,
            expected_revision=expected_revision,
            allowed={"paused", "retry_wait"},
            event_type="job.resume_requested",
        )

    async def _requeue(
        self,
        job_id: str,
        *,
        expected_revision: int,
        allowed: set[DurableJobStatus],
        event_type: str,
    ) -> DurableJobRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._requeue_sync, job_id, expected_revision, allowed, event_type
            )

    def _requeue_sync(
        self,
        job_id: str,
        expected_revision: int,
        allowed: set[DurableJobStatus],
        event_type: str,
    ) -> DurableJobRecord:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            record = self._job_from_row(dict(row))
            if record.revision != expected_revision:
                raise DurableJobConflict(
                    f"durable job revision conflict: expected {expected_revision}, current {record.revision}"
                )
            if record.status not in allowed:
                raise DurableJobConflict(f"durable job cannot be requeued from {record.status}")
            revision = record.revision + 1
            max_attempts = max(record.max_attempts, record.attempt_count + 1)
            conn.execute(
                """UPDATE durable_jobs SET status='queued',next_attempt_at=?,run_id=NULL,
                   platform_task_id=NULL,lease_owner=NULL,lease_expires_at=NULL,error='',alert_json=NULL,
                   cancel_requested=0,max_attempts=?,revision=?,updated_at=?,finished_at=NULL
                   WHERE id=? AND revision=?""",
                (now, max_attempts, revision, now, job_id, record.revision),
            )
            self._append_event_sync(conn, job_id, event_type, {}, now)
            updated = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_from_row(dict(updated))

    async def list_events(
        self, job_id: str, *, limit: int = 200, offset: int = 0
    ) -> list[DurableJobEvent]:
        return await asyncio.to_thread(
            self._list_events_sync,
            job_id,
            max(1, min(limit, 1000)),
            max(0, offset),
        )

    def _list_events_sync(
        self, job_id: str, limit: int, offset: int
    ) -> list[DurableJobEvent]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM durable_job_events WHERE job_id=?
                   ORDER BY sequence ASC LIMIT ? OFFSET ?""",
                (job_id, limit, offset),
            ).fetchall()
            return [
                DurableJobEvent(
                    sequence=row["sequence"],
                    job_id=row["job_id"],
                    event_type=row["event_type"],
                    data=json.loads(row["data_json"]),
                    created_at=row["created_at"],
                )
                for row in rows
            ]

    async def list_attempts(self, job_id: str) -> list[DurableJobAttempt]:
        return await asyncio.to_thread(self._list_attempts_sync, job_id)

    def _list_attempts_sync(self, job_id: str) -> list[DurableJobAttempt]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM durable_job_attempts WHERE job_id=? ORDER BY attempt_number",
                (job_id,),
            ).fetchall()
            return [DurableJobAttempt.model_validate(dict(row)) for row in rows]

    async def receipt_for_source(
        self, job_id: str, source_key: str
    ) -> CollectionReceipt | None:
        return await asyncio.to_thread(self._receipt_for_source_sync, job_id, source_key)

    def _receipt_for_source_sync(
        self, job_id: str, source_key: str
    ) -> CollectionReceipt | None:
        with self.storage._connect() as conn:
            row = conn.execute(
                "SELECT * FROM collection_receipts WHERE job_id=? AND source_key=?",
                (job_id, source_key),
            ).fetchone()
            return self._receipt_from_row(dict(row)) if row else None

    async def latest_receipt(
        self, application_id: str, canonical_url: str, *, exclude_job_id: str = ""
    ) -> CollectionReceipt | None:
        return await asyncio.to_thread(
            self._latest_receipt_sync, application_id, canonical_url, exclude_job_id
        )

    def _latest_receipt_sync(
        self, application_id: str, canonical_url: str, exclude_job_id: str
    ) -> CollectionReceipt | None:
        with self.storage._connect() as conn:
            if exclude_job_id:
                row = conn.execute(
                    """SELECT * FROM collection_receipts
                       WHERE application_id=? AND canonical_url=? AND job_id<>?
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (application_id, canonical_url, exclude_job_id),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM collection_receipts
                       WHERE application_id=? AND canonical_url=?
                       ORDER BY created_at DESC, id DESC LIMIT 1""",
                    (application_id, canonical_url),
                ).fetchone()
            return self._receipt_from_row(dict(row)) if row else None

    async def save_receipt(
        self,
        receipt: CollectionReceipt,
        *,
        worker_id: str,
        lease_version: int,
    ) -> CollectionReceipt:
        async with self._lock:
            return await asyncio.to_thread(
                self._save_receipt_sync, receipt, worker_id, lease_version
            )

    def _save_receipt_sync(
        self,
        receipt: CollectionReceipt,
        worker_id: str,
        lease_version: int,
    ) -> CollectionReceipt:
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (receipt.job_id,)).fetchone()
            if not row:
                raise KeyError(receipt.job_id)
            self._assert_fence(self._job_from_row(dict(row)), worker_id, lease_version)
            values = receipt.model_dump(mode="json")
            conn.execute(
                """INSERT INTO collection_receipts
                   (id,job_id,application_id,run_id,source_key,requested_url,final_url,
                    canonical_url,host,permission_basis,robots_checked,robots_allowed,status,
                    http_status,content_type,content_bytes,content_hash,previous_receipt_id,
                    previous_content_hash,title,excerpt,transformation_json,error,collected_at,
                    created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(job_id,source_key) DO UPDATE SET
                     run_id=excluded.run_id,final_url=excluded.final_url,canonical_url=excluded.canonical_url,
                     host=excluded.host,permission_basis=excluded.permission_basis,
                     robots_checked=excluded.robots_checked,robots_allowed=excluded.robots_allowed,
                     status=excluded.status,http_status=excluded.http_status,
                     content_type=excluded.content_type,content_bytes=excluded.content_bytes,
                     content_hash=excluded.content_hash,previous_receipt_id=excluded.previous_receipt_id,
                     previous_content_hash=excluded.previous_content_hash,title=excluded.title,
                     excerpt=excluded.excerpt,transformation_json=excluded.transformation_json,
                     error=excluded.error,collected_at=excluded.collected_at,updated_at=excluded.updated_at""",
                (
                    values["id"],
                    values["job_id"],
                    values["application_id"],
                    values["run_id"],
                    values["source_key"],
                    values["requested_url"],
                    values["final_url"],
                    values["canonical_url"],
                    values["host"],
                    values["permission_basis"],
                    int(values["robots_checked"]),
                    None if values["robots_allowed"] is None else int(values["robots_allowed"]),
                    values["status"],
                    values["http_status"],
                    values["content_type"],
                    values["content_bytes"],
                    values["content_hash"],
                    values["previous_receipt_id"],
                    values["previous_content_hash"],
                    values["title"],
                    values["excerpt"],
                    json.dumps(values["transformation"]),
                    values["error"],
                    values["collected_at"],
                    values["created_at"],
                    values["updated_at"],
                ),
            )
            self._append_event_sync(
                conn,
                receipt.job_id,
                "collection.receipt_saved",
                {
                    "receipt_id": receipt.id,
                    "source_key": receipt.source_key,
                    "status": receipt.status,
                    "canonical_url": receipt.canonical_url,
                },
                receipt.updated_at,
            )
            saved = conn.execute(
                "SELECT * FROM collection_receipts WHERE job_id=? AND source_key=?",
                (receipt.job_id, receipt.source_key),
            ).fetchone()
            return self._receipt_from_row(dict(saved))

    async def list_receipts(
        self, job_id: str, *, limit: int = 200, offset: int = 0
    ) -> list[CollectionReceipt]:
        return await asyncio.to_thread(
            self._list_receipts_sync,
            job_id,
            max(1, min(limit, 1000)),
            max(0, offset),
        )

    def _list_receipts_sync(
        self, job_id: str, limit: int, offset: int
    ) -> list[CollectionReceipt]:
        with self.storage._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM collection_receipts WHERE job_id=?
                   ORDER BY created_at, source_key LIMIT ? OFFSET ?""",
                (job_id, limit, offset),
            ).fetchall()
            return [self._receipt_from_row(dict(row)) for row in rows]

    async def append_event(
        self, job_id: str, event_type: str, data: dict[str, Any]
    ) -> DurableJobEvent:
        return await asyncio.to_thread(self._append_event_public_sync, job_id, event_type, data)

    def _append_event_public_sync(
        self, job_id: str, event_type: str, data: dict[str, Any]
    ) -> DurableJobEvent:
        now = utc_now()
        with self.storage._connect() as conn:
            cursor = self._append_event_sync(conn, job_id, event_type, data, now)
            return DurableJobEvent(
                sequence=int(cursor.lastrowid),
                job_id=job_id,
                event_type=event_type,
                data=data,
                created_at=now,
            )

    async def _fenced_update(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_version: int,
        event_type: str,
        updates: dict[str, Any],
        event_data: dict[str, Any] | None = None,
    ) -> DurableJobRecord:
        async with self._lock:
            return await asyncio.to_thread(
                self._fenced_update_sync,
                job_id,
                worker_id,
                lease_version,
                event_type,
                updates,
                event_data or {},
            )

    def _fenced_update_sync(
        self,
        job_id: str,
        worker_id: str,
        lease_version: int,
        event_type: str,
        updates: dict[str, Any],
        event_data: dict[str, Any],
    ) -> DurableJobRecord:
        now = utc_now()
        with self.storage._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            record = self._job_from_row(dict(row))
            self._assert_fence(record, worker_id, lease_version)
            column_values = {**updates, "updated_at": now, "revision": record.revision + 1}
            assignments = ",".join(f"{column}=?" for column in column_values)
            values = [*column_values.values(), job_id, record.revision]
            cursor = conn.execute(
                f"UPDATE durable_jobs SET {assignments} WHERE id=? AND revision=?", values
            )
            if cursor.rowcount != 1:
                raise DurableJobConflict("durable job changed during fenced update")
            self._append_event_sync(conn, job_id, event_type, event_data, now)
            updated = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (job_id,)).fetchone()
            return self._job_from_row(dict(updated))

    @staticmethod
    def _assert_fence(record: DurableJobRecord, worker_id: str, lease_version: int) -> None:
        if record.status != "running":
            raise DurableJobConflict(f"durable job is not running: {record.status}")
        if record.lease_owner != worker_id or record.lease_version != lease_version:
            raise DurableJobConflict(
                "durable job lease fence rejected stale owner or version"
            )
        if record.lease_expires_at and DurableJobStore._parse_time(record.lease_expires_at) <= datetime.now(
            timezone.utc
        ):
            raise DurableJobConflict("durable job lease has expired")

    def _finish_attempt_sync(
        self, record: DurableJobRecord, status: str, error: str
    ) -> None:
        self.storage._execute(
            """UPDATE durable_job_attempts SET status=?,error=?,finished_at=?
               WHERE job_id=? AND attempt_number=?""",
            (status, error, utc_now(), record.id, record.attempt_count),
        )

    @staticmethod
    def _append_event_sync(
        conn: Any,
        job_id: str,
        event_type: str,
        data: dict[str, Any],
        created_at: str,
    ) -> Any:
        return conn.execute(
            """INSERT INTO durable_job_events(job_id,event_type,data_json,created_at)
               VALUES(?,?,?,?)""",
            (job_id, event_type, json.dumps(data), created_at),
        )

    @staticmethod
    def _job_from_row(row: dict[str, Any]) -> DurableJobRecord:
        return DurableJobRecord(
            id=row["id"],
            idempotency_key=row["idempotency_key"],
            application_id=row["application_id"],
            version=int(row["version"]),
            node_id=row["node_id"],
            trigger_kind=row["trigger_kind"],
            local_date=row["local_date"],
            status=row["status"],
            payload=json.loads(row["payload_json"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            retry_backoff_seconds=float(row["retry_backoff_seconds"]),
            next_attempt_at=row["next_attempt_at"],
            lease_owner=row["lease_owner"],
            lease_expires_at=row["lease_expires_at"],
            lease_version=int(row["lease_version"]),
            run_id=row["run_id"],
            platform_task_id=row["platform_task_id"],
            checkpoint=json.loads(row["checkpoint_json"]),
            result=json.loads(row["result_json"]),
            error=row["error"],
            alert=json.loads(row["alert_json"]) if row["alert_json"] else None,
            cancel_requested=bool(row["cancel_requested"]),
            revision=int(row["revision"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _receipt_from_row(row: dict[str, Any]) -> CollectionReceipt:
        return CollectionReceipt(
            id=row["id"],
            job_id=row["job_id"],
            application_id=row["application_id"],
            run_id=row["run_id"],
            source_key=row["source_key"],
            requested_url=row["requested_url"],
            final_url=row["final_url"],
            canonical_url=row["canonical_url"],
            host=row["host"],
            permission_basis=row["permission_basis"],
            robots_checked=bool(row["robots_checked"]),
            robots_allowed=None if row["robots_allowed"] is None else bool(row["robots_allowed"]),
            status=row["status"],
            http_status=row["http_status"],
            content_type=row["content_type"],
            content_bytes=int(row["content_bytes"]),
            content_hash=row["content_hash"],
            previous_receipt_id=row["previous_receipt_id"],
            previous_content_hash=row["previous_content_hash"],
            title=row["title"],
            excerpt=row["excerpt"],
            transformation=json.loads(row["transformation_json"]),
            error=row["error"],
            collected_at=row["collected_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def durable_job_id(idempotency_key: str) -> str:
    if not idempotency_key:
        return f"job-{uuid4()}"
    return "job-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
