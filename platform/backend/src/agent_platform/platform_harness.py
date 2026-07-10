from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .models import utc_now
from .storage import Storage


TaskKind = Literal[
    "workflow_run",
    "builder_build",
    "test_suite",
    "scheduler_trigger",
    "scheduler_manual_trigger",
    "benchmark",
    "draft_patch_preview",
]
TaskStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
UsageType = Literal[
    "node_execution",
    "model_call",
    "tool_call",
    "nested_workflow_call",
    "scheduler_fire",
]
SECRET_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "cookie",
)
SECRET_REFERENCE_KEYS = ("$secret", "secret_ref")
SECRET_ENVELOPE_PREFIX = "secret-envelope:v1:"
SECRET_ENVELOPE_ITERATIONS = 200_000


class PlatformHarnessViolation(RuntimeError):
    pass


class PlatformUsageRecord(BaseModel):
    usage_type: UsageType
    amount: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now)


class PlatformTaskRecord(BaseModel):
    id: str
    kind: TaskKind
    owner_id: str
    resource_id: str
    status: TaskStatus = "queued"
    parent_task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    usage_counts: dict[str, int] = Field(default_factory=dict)
    usage: list[PlatformUsageRecord] = Field(default_factory=list)
    error: str = ""
    worker_id: str | None = None
    lease_expires_at: str | None = None
    lease_version: int = 0
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None


class PlatformHarness:
    """Platform Harness task monitor with durable task records.

    This is intentionally small: it gives Lilies a hard platform-side task
    boundary and resource counters with durable monitor records, without
    pretending to be a durable execution queue. Every transition is also
    emitted to the event store for audit.
    """

    def __init__(
        self,
        *,
        storage: Storage,
        max_active_tasks: int = 100,
        max_model_calls_per_task: int = 100,
        max_tool_calls_per_task: int = 200,
        max_node_executions_per_task: int = 1000,
        max_model_calls_per_owner: int = 0,
        max_tool_calls_per_owner: int = 0,
        max_node_executions_per_owner: int = 0,
        stale_active_task_seconds: float = 0.0,
        secret_policy_enabled: bool = True,
        secret_envelope_key: str = "",
        network_egress_policy: str = "full",
        network_egress_allowlist: list[str] | None = None,
        worker_id: str | None = None,
        worker_lease_seconds: float = 0.0,
    ) -> None:
        self.storage = storage
        self.max_active_tasks = max_active_tasks
        self.max_model_calls_per_task = max_model_calls_per_task
        self.max_tool_calls_per_task = max_tool_calls_per_task
        self.max_node_executions_per_task = max_node_executions_per_task
        self.max_model_calls_per_owner = max_model_calls_per_owner
        self.max_tool_calls_per_owner = max_tool_calls_per_owner
        self.max_node_executions_per_owner = max_node_executions_per_owner
        self.stale_active_task_seconds = stale_active_task_seconds
        self.secret_policy_enabled = secret_policy_enabled
        self.secret_envelope_key = secret_envelope_key
        self.network_egress_policy = network_egress_policy
        self.network_egress_allowlist = network_egress_allowlist or []
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.worker_lease_seconds = max(0.0, worker_lease_seconds)
        self._tasks: dict[str, PlatformTaskRecord] = {}
        self._lock = asyncio.Lock()

    async def start_task(
        self,
        task_id: str,
        *,
        kind: TaskKind,
        owner_id: str,
        resource_id: str,
        parent_task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> PlatformTaskRecord:
        await self.reconcile_expired_task_leases()
        await self.reconcile_stale_tasks()
        effective_lease_seconds = self._effective_lease_seconds(lease_seconds)
        effective_worker_id = self._effective_worker_id(worker_id)
        existing = await self._cached_or_persisted_task(task_id)
        if existing:
            should_emit = False
            async with self._lock:
                record = self._tasks[task_id]
                if record.status == "paused":
                    record.status = "running"
                    record.updated_at = utc_now()
                    record.finished_at = None
                    if effective_lease_seconds > 0:
                        self._assign_lease(
                            record,
                            worker_id=effective_worker_id,
                            lease_seconds=effective_lease_seconds,
                            reason="resume",
                        )
                    should_emit = True
            if should_emit:
                await self._persist(record)
                await self._emit(record, "platform_harness.task.started")
            return record

        active = await self.storage.count_platform_tasks(statuses={"queued", "running"})
        async with self._lock:
            if task_id in self._tasks:
                return self._tasks[task_id]
            if active >= self.max_active_tasks:
                raise PlatformHarnessViolation(
                    f"platform harness active task limit exceeded: {active} >= {self.max_active_tasks}"
                )
            record = PlatformTaskRecord(
                id=task_id,
                kind=kind,
                owner_id=owner_id,
                resource_id=resource_id,
                status="running",
                parent_task_id=parent_task_id,
                metadata=metadata or {},
            )
            if effective_lease_seconds > 0:
                self._assign_lease(
                    record,
                    worker_id=effective_worker_id,
                    lease_seconds=effective_lease_seconds,
                    reason="start",
                )
            self._tasks[task_id] = record
        await self._persist(record)
        await self._emit(record, "platform_harness.task.started")
        return record

    async def record_usage(
        self,
        task_id: str,
        usage_type: UsageType,
        *,
        amount: int = 1,
        metadata: dict[str, Any] | None = None,
    ) -> PlatformUsageRecord:
        await self._cached_or_persisted_task(task_id)
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise PlatformHarnessViolation(f"platform task not registered: {task_id}")
            if record.status not in {"queued", "running"}:
                raise PlatformHarnessViolation(
                    f"platform task is not running: {task_id} status={record.status}"
                )
            owner_id = record.owner_id

        owner_violation = await self._owner_violation(owner_id, usage_type, amount)

        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise PlatformHarnessViolation(f"platform task not registered: {task_id}")
            if record.status not in {"queued", "running"}:
                raise PlatformHarnessViolation(
                    f"platform task is not running: {task_id} status={record.status}"
                )
            lease_error = self._lease_expired_error(record)
            if lease_error:
                violation = lease_error
                usage = PlatformUsageRecord(
                    usage_type=usage_type,
                    amount=amount,
                    metadata=metadata or {},
                )
                record.usage.append(usage)
                record.usage_counts[usage_type] = record.usage_counts.get(usage_type, 0) + amount
                record.updated_at = utc_now()
                self._fail_for_expired_lease(record, lease_error)
            else:
                usage = PlatformUsageRecord(
                    usage_type=usage_type,
                    amount=amount,
                    metadata=metadata or {},
                )
                record.usage.append(usage)
                record.usage_counts[usage_type] = record.usage_counts.get(usage_type, 0) + amount
                record.updated_at = utc_now()
                violation = self._violation(record, usage_type) or owner_violation
                if violation:
                    record.status = "failed"
                    record.error = violation
                    record.finished_at = record.updated_at
        await self._persist(record)
        await self._emit(record, "platform_harness.usage.recorded", usage.model_dump(mode="json"))
        if violation:
            await self._emit(record, "platform_harness.violation", {"error": violation})
            raise PlatformHarnessViolation(violation)
        return usage

    async def finish_task(
        self,
        task_id: str,
        *,
        status: TaskStatus,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PlatformTaskRecord | None:
        await self._cached_or_persisted_task(task_id)
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                return None
            lease_error = self._lease_expired_error(record)
            if status == "succeeded" and lease_error:
                record.updated_at = utc_now()
                self._fail_for_expired_lease(record, lease_error)
                event_status = "failed"
            else:
                record.status = status
                record.error = error
                record.updated_at = utc_now()
                record.finished_at = record.updated_at
                if metadata:
                    record.metadata.update(metadata)
                event_status = status
        await self._persist(record)
        await self._emit(record, f"platform_harness.task.{event_status}")
        if event_status == "failed" and status == "succeeded" and lease_error:
            await self._emit(record, "platform_harness.violation", {"error": lease_error})
        return record

    async def get_task(self, task_id: str) -> PlatformTaskRecord:
        record = await self._cached_or_persisted_task(task_id)
        if not record:
            raise KeyError(f"platform task not found: {task_id}") from None
        return record.model_copy(deep=True)

    async def list_tasks(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[PlatformTaskRecord]:
        await self.reconcile_expired_task_leases()
        await self.reconcile_stale_tasks()
        rows = await self.storage.list_platform_tasks(
            kind=kind,
            status=status,
            owner_id=owner_id,
            limit=limit,
        )
        tasks = [PlatformTaskRecord.model_validate(item) for item in rows]
        async with self._lock:
            for task in tasks:
                self._tasks.setdefault(task.id, task)
        tasks.sort(key=lambda item: item.created_at, reverse=True)
        return [item.model_copy(deep=True) for item in tasks[:limit]]

    async def claim_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> PlatformTaskRecord:
        await self.reconcile_expired_task_leases()
        record = await self._change_task_lease(
            task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            action="claimed",
        )
        await self._emit(record, "platform_harness.task.lease_claimed")
        return record

    async def renew_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> PlatformTaskRecord:
        await self.reconcile_expired_task_leases()
        record = await self._change_task_lease(
            task_id,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            action="renewed",
            require_existing_worker=True,
        )
        await self._emit(record, "platform_harness.task.lease_renewed")
        return record

    async def release_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        next_status: Literal["queued", "running"] = "queued",
    ) -> PlatformTaskRecord:
        await self.reconcile_expired_task_leases()
        await self._cached_or_persisted_task(task_id)
        effective_worker_id = self._effective_worker_id(worker_id)
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(f"platform task not found: {task_id}") from None
            if record.status not in {"queued", "running"}:
                raise PlatformHarnessViolation(
                    f"platform task cannot release lease: {task_id} status={record.status}"
                )
            if (
                record.worker_id
                and record.worker_id != effective_worker_id
                and not self._lease_expired_error(record)
            ):
                raise PlatformHarnessViolation(
                    f"platform task lease held by {record.worker_id}; {effective_worker_id} cannot release it"
                )
            record.worker_id = None
            record.lease_expires_at = None
            record.lease_version += 1
            record.status = next_status
            record.updated_at = utc_now()
            metadata = record.metadata.setdefault("worker_lease", {})
            metadata.update({
                "released_at": record.updated_at,
                "released_by": effective_worker_id,
                "next_status": next_status,
            })
        await self._persist(record)
        await self._emit(record, "platform_harness.task.lease_released")
        return record.model_copy(deep=True)

    async def reconcile_expired_task_leases(self) -> list[PlatformTaskRecord]:
        cutoff = datetime.now(timezone.utc).isoformat()
        error = "platform harness worker lease expired"
        records = [
            PlatformTaskRecord.model_validate(item)
            for item in await self.storage.fail_expired_platform_task_leases(cutoff=cutoff, error=error)
        ]
        async with self._lock:
            for record in records:
                self._tasks[record.id] = record
        for record in records:
            await self._emit(record, "platform_harness.task.failed", {"reason": "worker_lease_expired"})
        return [record.model_copy(deep=True) for record in records]

    async def reconcile_stale_tasks(self) -> list[PlatformTaskRecord]:
        if self.stale_active_task_seconds <= 0:
            return []
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=self.stale_active_task_seconds)
        ).isoformat()
        error = f"platform harness active task stale for more than {self.stale_active_task_seconds:g}s"
        records = [
            PlatformTaskRecord.model_validate(item)
            for item in await self.storage.fail_stale_platform_tasks(cutoff=cutoff, error=error)
        ]
        async with self._lock:
            for record in records:
                self._tasks[record.id] = record
        for record in records:
            await self._emit(record, "platform_harness.task.failed", {"reason": "stale_reconciled"})
        return [record.model_copy(deep=True) for record in records]

    async def _change_task_lease(
        self,
        task_id: str,
        *,
        worker_id: str | None,
        lease_seconds: float | None,
        action: str,
        require_existing_worker: bool = False,
    ) -> PlatformTaskRecord:
        await self._cached_or_persisted_task(task_id)
        effective_lease_seconds = self._effective_lease_seconds(lease_seconds)
        if effective_lease_seconds <= 0:
            raise PlatformHarnessViolation("platform task worker lease seconds must be greater than 0")
        effective_worker_id = self._effective_worker_id(worker_id)
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(f"platform task not found: {task_id}") from None
            if record.status not in {"queued", "running"}:
                raise PlatformHarnessViolation(
                    f"platform task cannot change lease: {task_id} status={record.status}"
                )
            if require_existing_worker and not record.worker_id:
                raise PlatformHarnessViolation(f"platform task has no active worker lease: {task_id}")
            if require_existing_worker and record.worker_id and record.worker_id != effective_worker_id:
                raise PlatformHarnessViolation(
                    f"platform task lease held by {record.worker_id}; {effective_worker_id} cannot renew it"
                )
            if (
                not require_existing_worker
                and record.worker_id
                and record.worker_id != effective_worker_id
                and not self._lease_expired_error(record)
            ):
                raise PlatformHarnessViolation(
                    f"platform task lease held by {record.worker_id}; {effective_worker_id} cannot claim it"
                )
            self._assign_lease(
                record,
                worker_id=effective_worker_id,
                lease_seconds=effective_lease_seconds,
                reason=action,
            )
            if record.status == "queued":
                record.status = "running"
        await self._persist(record)
        return record.model_copy(deep=True)

    def _violation(self, record: PlatformTaskRecord, usage_type: UsageType) -> str:
        counts = record.usage_counts
        if usage_type == "model_call" and counts.get("model_call", 0) > self.max_model_calls_per_task:
            return (
                "model call budget exceeded: "
                f"{counts['model_call']} > {self.max_model_calls_per_task}"
            )
        if usage_type == "tool_call" and counts.get("tool_call", 0) > self.max_tool_calls_per_task:
            return (
                "tool call budget exceeded: "
                f"{counts['tool_call']} > {self.max_tool_calls_per_task}"
            )
        if (
            usage_type == "node_execution"
            and counts.get("node_execution", 0) > self.max_node_executions_per_task
        ):
            return (
                "node execution budget exceeded: "
                f"{counts['node_execution']} > {self.max_node_executions_per_task}"
            )
        return ""

    async def _owner_violation(self, owner_id: str, usage_type: UsageType, amount: int) -> str:
        limit = self._owner_limit(usage_type)
        if limit <= 0:
            return ""
        used = await self.storage.sum_platform_usage_count(
            owner_id=owner_id,
            usage_type=usage_type,
        )
        total = used + amount
        if total <= limit:
            return ""
        label = {
            "model_call": "model call",
            "tool_call": "tool call",
            "node_execution": "node execution",
        }.get(usage_type, usage_type.replace("_", " "))
        return f"owner {label} budget exceeded: {total} > {limit}"

    def _owner_limit(self, usage_type: UsageType) -> int:
        if usage_type == "model_call":
            return self.max_model_calls_per_owner
        if usage_type == "tool_call":
            return self.max_tool_calls_per_owner
        if usage_type == "node_execution":
            return self.max_node_executions_per_owner
        return 0

    def _effective_worker_id(self, worker_id: str | None) -> str:
        return worker_id or self.worker_id

    def _effective_lease_seconds(self, lease_seconds: float | None) -> float:
        if lease_seconds is None:
            return self.worker_lease_seconds
        return max(0.0, float(lease_seconds))

    def _assign_lease(
        self,
        record: PlatformTaskRecord,
        *,
        worker_id: str,
        lease_seconds: float,
        reason: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        record.worker_id = worker_id
        record.lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        record.lease_version += 1
        record.updated_at = now.isoformat()
        metadata = record.metadata.setdefault("worker_lease", {})
        metadata.update({
            "worker_id": worker_id,
            "lease_seconds": lease_seconds,
            "last_action": reason,
            "updated_at": record.updated_at,
            "lease_version": record.lease_version,
        })

    def _lease_expired_error(self, record: PlatformTaskRecord) -> str:
        if not record.lease_expires_at:
            return ""
        expires_at = self._parse_datetime(record.lease_expires_at)
        if not expires_at:
            return ""
        if expires_at > datetime.now(timezone.utc):
            return ""
        return (
            "platform harness worker lease expired"
            f": task={record.id} worker={record.worker_id or 'unknown'}"
            f" lease_expires_at={record.lease_expires_at}"
        )

    def _fail_for_expired_lease(self, record: PlatformTaskRecord, error: str) -> None:
        record.status = "failed"
        record.error = error
        record.finished_at = record.updated_at
        metadata = record.metadata.setdefault("worker_lease", {})
        metadata.update({
            "expired": True,
            "expired_worker_id": record.worker_id,
            "expired_at": record.updated_at,
            "lease_expires_at": record.lease_expires_at,
        })

    def _parse_datetime(self, value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def enforce_secret_policy(self, *, surface: str, payload: Any) -> None:
        if not self.secret_policy_enabled:
            return
        path = self._find_secret_field(payload)
        if not path:
            return
        raise PlatformHarnessViolation(
            f"secret policy blocked {surface}: forbidden secret field at {path}"
        )

    def _find_secret_field(self, value: Any, path: str = "$") -> str:
        if self.is_secret_reference(value):
            return ""
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key)
                key_folded = key_text.casefold().replace("-", "_")
                item_path = f"{path}.{key_text}"
                if self.is_secret_reference(item):
                    continue
                if any(marker in key_folded for marker in SECRET_FIELD_MARKERS) and item not in (None, ""):
                    return item_path
                nested = self._find_secret_field(item, item_path)
                if nested:
                    return nested
        if isinstance(value, list):
            for index, item in enumerate(value):
                nested = self._find_secret_field(item, f"{path}[{index}]")
                if nested:
                    return nested
        return ""

    async def save_secret(
        self,
        *,
        owner_id: str,
        name: str,
        value: str,
        description: str = "",
    ) -> dict[str, Any]:
        self._validate_secret_identity(owner_id, name)
        stored_value = self._encrypt_secret_value(value)
        row = await self.storage.save_platform_secret(
            owner_id=owner_id,
            name=name,
            value=stored_value,
            description=description,
        )
        await self.storage.append_event(
            owner_id,
            "platform_harness.secret.saved",
            {"secret": self._public_secret(row)},
        )
        return self._public_secret(row)

    async def list_secrets(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self.storage.list_platform_secrets(owner_id=owner_id)
        return [self._public_secret(row) for row in rows]

    async def delete_secret(self, *, owner_id: str, name: str) -> bool:
        self._validate_secret_identity(owner_id, name)
        deleted = await self.storage.delete_platform_secret(owner_id=owner_id, name=name)
        await self.storage.append_event(
            owner_id,
            "platform_harness.secret.deleted",
            {"owner_id": owner_id, "name": name, "deleted": deleted},
        )
        return deleted

    def is_secret_reference(self, value: Any) -> bool:
        return isinstance(value, dict) and any(key in value for key in SECRET_REFERENCE_KEYS)

    async def inject_secret_references(self, *, owner_id: str, payload: Any) -> Any:
        if self.is_secret_reference(payload):
            return await self._resolve_secret_reference(owner_id=owner_id, reference=payload)
        if isinstance(payload, dict):
            return {
                key: await self.inject_secret_references(owner_id=owner_id, payload=value)
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return [
                await self.inject_secret_references(owner_id=owner_id, payload=value)
                for value in payload
            ]
        return payload

    async def _resolve_secret_reference(self, *, owner_id: str, reference: dict[str, Any]) -> str:
        raw_ref = next((reference.get(key) for key in SECRET_REFERENCE_KEYS if reference.get(key)), "")
        ref_owner, name = self._split_secret_reference(str(raw_ref), owner_id)
        if reference.get("owner_id"):
            ref_owner = str(reference["owner_id"])
        self._validate_secret_identity(ref_owner, name)
        try:
            row = await self.storage.get_platform_secret(owner_id=ref_owner, name=name)
        except KeyError as error:
            raise PlatformHarnessViolation(str(error)) from error
        prefix = str(reference.get("prefix", ""))
        suffix = str(reference.get("suffix", ""))
        return f"{prefix}{self._decrypt_secret_value(str(row['value']))}{suffix}"

    def _encrypt_secret_value(self, value: str) -> str:
        if not self.secret_envelope_key:
            return value
        salt = os.urandom(16)
        nonce = os.urandom(16)
        enc_key, mac_key = self._derive_secret_envelope_keys(salt)
        plaintext = value.encode("utf-8")
        ciphertext = self._xor_bytes(plaintext, self._keystream(enc_key, nonce, len(plaintext)))
        envelope = {
            "algorithm": "hmac-sha256-xor-stream",
            "ciphertext": self._b64(ciphertext),
            "iterations": SECRET_ENVELOPE_ITERATIONS,
            "kdf": "pbkdf2-hmac-sha256",
            "nonce": self._b64(nonce),
            "salt": self._b64(salt),
            "version": 1,
        }
        mac_input = self._stable_json(envelope).encode("utf-8")
        envelope["tag"] = self._b64(hmac.new(mac_key, mac_input, hashlib.sha256).digest())
        return SECRET_ENVELOPE_PREFIX + self._b64(self._stable_json(envelope).encode("utf-8"))

    def _decrypt_secret_value(self, stored_value: str) -> str:
        if not stored_value.startswith(SECRET_ENVELOPE_PREFIX):
            return stored_value
        if not self.secret_envelope_key:
            raise PlatformHarnessViolation("platform secret envelope key is not configured")
        try:
            raw = self._unb64(stored_value.removeprefix(SECRET_ENVELOPE_PREFIX))
            envelope = json.loads(raw.decode("utf-8"))
            tag = self._unb64(str(envelope.pop("tag")))
            salt = self._unb64(str(envelope["salt"]))
            nonce = self._unb64(str(envelope["nonce"]))
            ciphertext = self._unb64(str(envelope["ciphertext"]))
        except Exception as error:
            raise PlatformHarnessViolation("platform secret envelope is invalid") from error
        enc_key, mac_key = self._derive_secret_envelope_keys(salt)
        mac_input = self._stable_json(envelope).encode("utf-8")
        expected = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise PlatformHarnessViolation("platform secret envelope authentication failed")
        plaintext = self._xor_bytes(ciphertext, self._keystream(enc_key, nonce, len(ciphertext)))
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PlatformHarnessViolation("platform secret envelope plaintext is invalid") from error

    def _derive_secret_envelope_keys(self, salt: bytes) -> tuple[bytes, bytes]:
        material = hashlib.pbkdf2_hmac(
            "sha256",
            self.secret_envelope_key.encode("utf-8"),
            salt,
            SECRET_ENVELOPE_ITERATIONS,
            dklen=64,
        )
        return material[:32], material[32:]

    def _keystream(self, key: bytes, nonce: bytes, length: int) -> bytes:
        chunks: list[bytes] = []
        counter = 0
        produced = 0
        while produced < length:
            counter_bytes = counter.to_bytes(8, "big")
            chunk = hmac.new(key, nonce + counter_bytes, hashlib.sha256).digest()
            chunks.append(chunk)
            produced += len(chunk)
            counter += 1
        return b"".join(chunks)[:length]

    def _xor_bytes(self, left: bytes, right: bytes) -> bytes:
        return bytes(a ^ b for a, b in zip(left, right, strict=True))

    def _stable_json(self, value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _b64(self, value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def _unb64(self, value: str) -> bytes:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))

    def _secret_storage_mode(self, stored_value: str) -> str:
        return "encrypted_v1" if stored_value.startswith(SECRET_ENVELOPE_PREFIX) else "legacy_plaintext"

    def _split_secret_reference(self, raw_ref: str, default_owner_id: str) -> tuple[str, str]:
        normalized = raw_ref.removeprefix("secret://").strip()
        if "/" in normalized:
            owner_id, name = normalized.split("/", 1)
            return owner_id, name
        return default_owner_id, normalized

    def _validate_secret_identity(self, owner_id: str, name: str) -> None:
        if not owner_id or "/" in owner_id or owner_id.strip() != owner_id:
            raise PlatformHarnessViolation("invalid platform secret owner_id")
        if not name or "/" in name or name.strip() != name:
            raise PlatformHarnessViolation("invalid platform secret name")

    def _public_secret(self, row: dict[str, Any]) -> dict[str, Any]:
        storage_mode = self._secret_storage_mode(str(row.get("value", "")))
        return {
            "id": row["id"],
            "owner_id": row["owner_id"],
            "name": row["name"],
            "description": row.get("description", ""),
            "secret_ref": f"secret://{row['owner_id']}/{row['name']}",
            "storage_mode": storage_mode,
            "encrypted": storage_mode.startswith("encrypted"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "redacted": True,
        }

    def enforce_network_egress_policy(self, *, surface: str, hostname: str) -> None:
        policy = self.network_egress_policy.casefold()
        if policy == "full":
            return
        if policy == "none":
            raise PlatformHarnessViolation(
                f"network egress policy blocked {surface}: outbound network is disabled"
            )
        if policy != "allowlist":
            raise PlatformHarnessViolation(f"unknown network egress policy: {self.network_egress_policy}")
        normalized = hostname.casefold().rstrip(".")
        allowed = [entry.casefold().rstrip(".") for entry in self.network_egress_allowlist]
        if any(normalized == entry or normalized.endswith(f".{entry}") for entry in allowed):
            return
        raise PlatformHarnessViolation(
            f"network egress policy blocked {surface}: host {hostname} is not allowlisted"
        )

    def enforce_stdio_mcp_policy(
        self,
        *,
        surface: str,
        server_name: str,
        agent_network_policy: Any,
        sandbox_network_policy: Any | None = None,
    ) -> None:
        decision = self.explain_stdio_mcp_policy(
            surface=surface,
            server_name=server_name,
            agent_network_policy=agent_network_policy,
            sandbox_network_policy=sandbox_network_policy,
        )
        if decision["allowed"]:
            return
        raise PlatformHarnessViolation(decision["reason"])

    def explain_stdio_mcp_policy(
        self,
        *,
        surface: str,
        server_name: str,
        agent_network_policy: Any,
        sandbox_network_policy: Any | None = None,
    ) -> dict[str, Any]:
        platform_policy = self._normalized_policy(self.network_egress_policy)
        agent_policy = self._normalized_policy(agent_network_policy)
        sandbox_policy = (
            self._normalized_policy(sandbox_network_policy)
            if sandbox_network_policy is not None
            else None
        )
        base = {
            "surface": surface,
            "server_name": server_name,
            "platform_policy": platform_policy,
            "agent_network_policy": agent_policy,
            "sandbox_network_policy": sandbox_policy,
            "allowed": False,
            "mode": "blocked",
            "reason": "",
            "operator_action": "",
        }
        if platform_policy == "full" and agent_policy == "full":
            return {
                **base,
                "allowed": True,
                "mode": "host_or_sandbox_full_network",
                "reason": (
                    f"stdio MCP allowed {surface}:{server_name}: platform and agent "
                    "network policies are both full"
                ),
                "operator_action": "Use only with trusted stdio MCP servers.",
            }
        if sandbox_policy == "none" and platform_policy in {"full", "none"} and agent_policy == "none":
            return {
                **base,
                "allowed": True,
                "mode": "sandboxed_no_network",
                "reason": (
                    f"stdio MCP allowed {surface}:{server_name}: execution is inside a "
                    "no-network sandbox boundary"
                ),
                "operator_action": "Keep the stdio server inside the sandbox runner.",
            }
        if sandbox_policy == "allowlist" or platform_policy == "allowlist" or agent_policy == "allowlist":
            reason = (
                "stdio MCP egress policy blocked "
                f"{surface}:{server_name}: stdio servers do not declare hostnames, "
                "so allowlist-grade enforcement requires hard sandbox/container firewalling"
            )
            action = (
                "Use an HTTP MCP server with a hostname allowlist, switch to a no-network "
                "sandbox for local stdio, or add hard sandbox firewalling before enabling stdio allowlist."
            )
        else:
            reason = (
                "stdio MCP egress policy blocked "
                f"{surface}:{server_name}: stdio servers do not declare hostnames; "
                "use full network policy or the sandboxed no-network stdio runner"
            )
            action = "Use full/full for trusted local stdio or sandboxed none/none for no-network stdio."
        return {**base, "reason": reason, "operator_action": action}

    def policy_controls(self) -> dict[str, Any]:
        decisions = [
            self._stdio_policy_control_decision(
                "trusted_full_network",
                "Trusted host or sandbox stdio",
                agent_policy="full",
                sandbox_policy=None,
            ),
            self._stdio_policy_control_decision(
                "sandboxed_no_network",
                "Sandboxed no-network stdio",
                agent_policy="none",
                sandbox_policy="none",
            ),
            self._stdio_policy_control_decision(
                "sandboxed_allowlist",
                "Sandboxed allowlist stdio",
                agent_policy="allowlist",
                sandbox_policy="allowlist",
            ),
            self._stdio_policy_control_decision(
                "restricted_unsandboxed",
                "Restricted unsandboxed stdio",
                agent_policy="none",
                sandbox_policy=None,
            ),
        ]
        return {
            "network_egress_policy": self._normalized_policy(self.network_egress_policy),
            "network_egress_allowlist": list(self.network_egress_allowlist),
            "secret_policy_enabled": self.secret_policy_enabled,
            "secret_storage": {
                "new_secret_mode": "encrypted_v1" if self.secret_envelope_key else "legacy_plaintext",
                "envelope_configured": bool(self.secret_envelope_key),
            },
            "worker_id": self.worker_id,
            "worker_lease_seconds": self.worker_lease_seconds,
            "limits": {
                "max_active_tasks": self.max_active_tasks,
                "max_model_calls_per_task": self.max_model_calls_per_task,
                "max_tool_calls_per_task": self.max_tool_calls_per_task,
                "max_node_executions_per_task": self.max_node_executions_per_task,
                "max_model_calls_per_owner": self.max_model_calls_per_owner,
                "max_tool_calls_per_owner": self.max_tool_calls_per_owner,
                "max_node_executions_per_owner": self.max_node_executions_per_owner,
            },
            "stdio_mcp": {
                "sandboxed_no_network_supported": True,
                "allowlist_supported": False,
                "decisions": decisions,
            },
            "e08_boundary": self._e08_boundary_summary(),
        }

    def _e08_boundary_summary(self) -> dict[str, Any]:
        network_policy = self._normalized_policy(self.network_egress_policy)
        budget_limits = {
            "max_model_calls_per_task": self.max_model_calls_per_task,
            "max_tool_calls_per_task": self.max_tool_calls_per_task,
            "max_node_executions_per_task": self.max_node_executions_per_task,
            "max_model_calls_per_owner": self.max_model_calls_per_owner,
            "max_tool_calls_per_owner": self.max_tool_calls_per_owner,
            "max_node_executions_per_owner": self.max_node_executions_per_owner,
        }
        return {
            "current_slice": "e08_policy_controls_surface",
            "source": "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            "comparison_evidence": "docs/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10_summary.md",
            "soft_passmode": {
                "layer": "workflow_internal",
                "enforcement": "soft_configurable",
                "statement": "workflow-internal passmode can pause or pass by workflow configuration",
            },
            "hard_boundary": {
                "layer": "platform_harness",
                "enforcement": "hard_boundary",
                "statement": "Platform Harness policy is enforced before external actions",
            },
            "not_full_sidecar_completion": True,
            "remaining_full_boundary": [
                "complete cancellation policy closure",
                "budget and owner-limit closure",
                "worker lease operator lifecycle",
                "editable policy controls",
                "full Studio/API operational runbook",
            ],
            "controls": [
                {
                    "id": "network_egress",
                    "label": "Network egress policy",
                    "layer": "platform_harness",
                    "status": "restricted" if network_policy != "full" else "open",
                    "value": network_policy,
                },
                {
                    "id": "secret_policy",
                    "label": "Secret policy",
                    "layer": "platform_harness",
                    "status": "enabled" if self.secret_policy_enabled else "disabled",
                    "value": self.secret_policy_enabled,
                },
                {
                    "id": "worker_lease",
                    "label": "Worker lease",
                    "layer": "platform_harness",
                    "status": "enabled" if self.worker_lease_seconds > 0 else "disabled",
                    "value": self.worker_lease_seconds,
                },
                {
                    "id": "budget_limits",
                    "label": "Task and owner budgets",
                    "layer": "platform_harness",
                    "status": "configured",
                    "value": budget_limits,
                },
                {
                    "id": "workflow_passmode",
                    "label": "Workflow passmode",
                    "layer": "workflow_internal",
                    "status": "soft_configurable",
                    "value": "permission_gate modes such as always_ask or auto_approve",
                },
            ],
            "behavior_matrix": self._e08_behavior_matrix(network_policy, budget_limits),
        }

    def _e08_behavior_matrix(self, network_policy: str, budget_limits: dict[str, int]) -> list[dict[str, Any]]:
        budget_configured = any(value > 0 for value in budget_limits.values())
        return [
            {
                "id": "workflow_passmode",
                "layer": "workflow_internal",
                "enforcement": "soft_configurable",
                "status": "available",
                "signal": "permission_gate modes can pause or pass by workflow configuration",
                "source": "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            },
            {
                "id": "cancellation_checkpoint",
                "layer": "workflow_runtime",
                "enforcement": "soft_checkpoint",
                "status": "available",
                "signal": "cancellation_point records a cancellable checkpoint and emits cancellation status",
                "source": "platform/backend/src/agent_platform/workflow_runtime.py",
            },
            {
                "id": "budget_limits",
                "layer": "platform_harness",
                "enforcement": "hard_counter",
                "status": "configured" if budget_configured else "disabled",
                "signal": "task and owner usage counters raise PlatformHarnessViolation when limits are exceeded",
                "source": "platform/backend/src/agent_platform/platform_harness.py",
            },
            {
                "id": "worker_lease",
                "layer": "platform_harness",
                "enforcement": "lease_coordination",
                "status": "enabled" if self.worker_lease_seconds > 0 else "disabled",
                "signal": "worker leases can expire, fail stale work, and be renewed by workers",
                "source": "platform/backend/src/agent_platform/platform_harness.py",
            },
            {
                "id": "network_egress_policy",
                "layer": "platform_harness",
                "enforcement": "hard_boundary",
                "status": "restricted" if network_policy != "full" else "open",
                "signal": "network egress policy blocks disallowed external actions before execution",
                "source": "platform/backend/src/agent_platform/platform_harness.py",
            },
            {
                "id": "secret_policy",
                "layer": "platform_harness",
                "enforcement": "hard_boundary",
                "status": "enabled" if self.secret_policy_enabled else "disabled",
                "signal": "secret policy blocks leaked secret material on governed surfaces",
                "source": "platform/backend/src/agent_platform/platform_harness.py",
            },
        ]

    def _stdio_policy_control_decision(
        self,
        decision_id: str,
        label: str,
        *,
        agent_policy: str,
        sandbox_policy: str | None,
    ) -> dict[str, Any]:
        return {
            "id": decision_id,
            "label": label,
            **self.explain_stdio_mcp_policy(
                surface="policy_controls",
                server_name=decision_id,
                agent_network_policy=agent_policy,
                sandbox_network_policy=sandbox_policy,
            ),
        }

    def _normalized_policy(self, value: Any) -> str:
        return str(getattr(value, "value", value)).casefold()

    async def _cached_or_persisted_task(self, task_id: str) -> PlatformTaskRecord | None:
        async with self._lock:
            record = self._tasks.get(task_id)
            if record is not None:
                return record
        try:
            data = await self.storage.get_platform_task(task_id)
        except KeyError:
            return None
        record = PlatformTaskRecord.model_validate(data)
        async with self._lock:
            return self._tasks.setdefault(task_id, record)

    async def _persist(self, record: PlatformTaskRecord) -> None:
        await self.storage.save_platform_task(record.model_dump(mode="json"))

    async def _emit(
        self, record: PlatformTaskRecord, event_type: str, extra: dict[str, Any] | None = None
    ) -> None:
        data = {
            "task": record.model_dump(mode="json"),
            **(extra or {}),
        }
        await self.storage.append_event("platform_harness", event_type, data)
        await self.storage.append_event(record.owner_id, event_type, data)
