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

from .models import Usage, utc_now
from .secret_kms import SecretKMSProvider
from .storage import Storage


TaskKind = Literal[
    "workflow_run",
    "builder_build",
    "agent_generation",
    "agent_turn",
    "test_suite",
    "scheduler_trigger",
    "scheduler_manual_trigger",
    "benchmark",
    "draft_patch_preview",
    "requirement_intake",
    "evaluation_run",
    "acceptance_pm",
]
TaskStatus = Literal["queued", "running", "paused", "succeeded", "failed", "cancelled"]
WorkerHeartbeatStatus = Literal["idle", "running", "stopping", "failed"]
UsageType = Literal[
    "node_execution",
    "model_call",
    "model_usage",
    "tool_call",
    # 平台自己发起的工具调用（状态机读草稿/跑校验/替模型查手册等）。
    # 单独计一类而不是并进 tool_call：工具预算约束的是**模型的行为**，
    # 平台的自查自纠越勤快，模型可用的额度就越少——这条耦合让机械引擎
    # 越完善越容易撞预算（真机 7d5ffa06：tool_call 201/200 判死，其中
    # 每步都有 draft_inspect + draft_validate，还有硬门代劳的 catalog_get）。
    # 仍然全量落账，只是不计入模型的工具预算。
    "platform_tool_call",
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
SECRET_ENVELOPE_V2_PREFIX = "secret-envelope:v2:"
SECRET_ENVELOPE_V3_PREFIX = "secret-envelope:v3:"
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
    # 每次复活（返修/复工）时的用量快照：预算按"本工作段增量"检查，
    # 而审计账（usage/usage_counts）保留完整生命周期。没有它，跨多段
    # 返修的长寿命构建必然撞任务级预算（ERP 盲测 204>200 处决案）。
    usage_baseline: dict[str, int] = Field(default_factory=dict)
    usage: list[PlatformUsageRecord] = Field(default_factory=list)
    error: str = ""
    worker_id: str | None = None
    lease_expires_at: str | None = None
    lease_version: int = 0
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None


class PlatformWorkerHeartbeatRecord(BaseModel):
    worker_id: str
    status: WorkerHeartbeatStatus = "idle"
    active_task_id: str = ""
    last_seen_at: str = Field(default_factory=utc_now)
    stale_after_seconds: float = Field(default=120.0, gt=0)
    liveness: Literal["active", "stale"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)
    updated_at: str = Field(default_factory=utc_now)


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
        secret_envelope_key_id: str = "local",
        secret_envelope_previous_keys: dict[str, str] | None = None,
        secret_kms_provider: SecretKMSProvider | None = None,
        network_egress_policy: str = "full",
        network_egress_allowlist: list[str] | None = None,
        cancellation_policy: str = "enabled",
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
        self.secret_envelope_key_id = self._normalized_secret_key_id(secret_envelope_key_id)
        self.secret_envelope_keyring = self._secret_envelope_keyring(
            current_key_id=self.secret_envelope_key_id,
            current_key=secret_envelope_key,
            previous_keys=secret_envelope_previous_keys or {},
        )
        self.secret_kms_provider = secret_kms_provider
        self.network_egress_policy = network_egress_policy
        self.network_egress_allowlist = network_egress_allowlist or []
        self.cancellation_policy = self._normalized_cancellation_policy(cancellation_policy)
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
                if record.status in {"paused", "failed", "cancelled", "succeeded"}:
                    # Terminal tasks restart in place: resume-after-failure is a
                    # first-class flow, and the event history keeps the record.
                    record.status = "running"
                    record.error = ""
                    record.updated_at = utc_now()
                    record.finished_at = None
                    # 新工作段：预算从当前累计重新起算（审计账不动）。
                    record.usage_baseline = dict(record.usage_counts)
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

    async def record_model_usage(
        self,
        task_id: str,
        usage: Usage,
        *,
        model: str,
        provider: str,
        metadata: dict[str, Any] | None = None,
        budget_limit_usd: float | None = None,
    ) -> PlatformUsageRecord:
        """Persist provider response usage without treating call counts as tokens."""
        await self._cached_or_persisted_task(task_id)
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise PlatformHarnessViolation(f"platform task not registered: {task_id}")
            task_snapshot = task.model_copy(deep=True)

        dimensions = dict(metadata or {})
        application_id = str(
            dimensions.get("application_id")
            or task_snapshot.metadata.get("application_id")
            or self._application_id_for_task(task_snapshot)
            or ""
        )
        workflow_id = str(
            dimensions.get("workflow_id")
            or task_snapshot.metadata.get("workflow_id")
            or application_id
            or ""
        )
        support = {
            field: usage.field_support.get(field, "not_reported")
            for field in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "reasoning_tokens",
            )
        }
        cost_value: float | None = None
        if usage.cost_source != "unsupported":
            cost_value = float(usage.cost_usd)
            support["cost_usd"] = (
                "reported" if usage.cost_source == "provider_reported" else "estimated"
            )
        else:
            support["cost_usd"] = "unsupported"

        prior_cost = 0.0
        for item in task_snapshot.usage:
            if item.usage_type != "model_usage":
                continue
            raw_cost = item.metadata.get("cost_usd")
            if isinstance(raw_cost, (int, float)):
                prior_cost += float(raw_cost)
        spent_cost = prior_cost + (cost_value or 0.0)
        raw_limit = (
            budget_limit_usd
            if budget_limit_usd is not None
            else task_snapshot.metadata.get("budget_limit_usd")
        )
        normalized_limit = (
            float(raw_limit)
            if isinstance(raw_limit, (int, float)) and float(raw_limit) >= 0
            else None
        )
        budget = {
            "limit_usd": normalized_limit,
            "spent_usd": spent_cost if cost_value is not None or prior_cost else None,
            "remaining_usd": (
                normalized_limit - spent_cost
                if normalized_limit is not None and (cost_value is not None or prior_cost)
                else None
            ),
            "exhausted": (
                spent_cost >= normalized_limit
                if normalized_limit is not None and (cost_value is not None or prior_cost)
                else None
            ),
            "support": (
                "reported_or_estimated"
                if normalized_limit is not None and (cost_value is not None or prior_cost)
                else "not_configured" if normalized_limit is None else "cost_unsupported"
            ),
        }
        payload = {
            **dimensions,
            "task_id": task_snapshot.id,
            "task_kind": task_snapshot.kind,
            "owner_id": task_snapshot.owner_id,
            "resource_id": task_snapshot.resource_id,
            "application_id": application_id or None,
            "workflow_id": workflow_id or None,
            "provider": provider,
            "model": model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "reasoning_tokens": usage.reasoning_tokens,
            "cost_usd": cost_value,
            "cost_source": usage.cost_source,
            "support": support,
            "budget": budget,
        }
        return await self.record_usage(
            task_id,
            "model_usage",
            metadata=payload,
        )

    @staticmethod
    def _application_id_for_task(task: PlatformTaskRecord) -> str:
        if task.kind in {
            "workflow_run",
            "builder_build",
            "test_suite",
            "evaluation_run",
            "scheduler_trigger",
            "scheduler_manual_trigger",
            "draft_patch_preview",
        }:
            return task.owner_id
        return ""

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
            if metadata:
                record.metadata.update(metadata)
            lease_error = self._lease_expired_error(record)
            if status == "succeeded" and lease_error:
                record.updated_at = utc_now()
                self._fail_for_expired_lease(record, lease_error)
                worker_metadata = record.metadata.get("worker_runner")
                if isinstance(worker_metadata, dict):
                    worker_metadata["status"] = "failed"
                    worker_metadata["completion_error"] = lease_error
                event_status = "failed"
            else:
                record.status = status
                record.error = error
                record.updated_at = utc_now()
                record.finished_at = record.updated_at
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

    async def claim_next_queued_task(
        self,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
        kind: str | None = None,
        owner_id: str | None = None,
    ) -> PlatformTaskRecord | None:
        await self.requeue_expired_task_leases()
        effective_lease_seconds = self._effective_lease_seconds(lease_seconds)
        if effective_lease_seconds <= 0:
            raise PlatformHarnessViolation("platform queue claim lease seconds must be greater than 0")
        effective_worker_id = self._effective_worker_id(worker_id)
        claimed = await self.storage.claim_next_platform_task(
            worker_id=effective_worker_id,
            lease_seconds=effective_lease_seconds,
            kind=kind,
            owner_id=owner_id,
        )
        if claimed is None:
            return None
        record = PlatformTaskRecord.model_validate(claimed)
        async with self._lock:
            self._tasks[record.id] = record
        await self._emit(record, "platform_harness.queue.task_claimed")
        return record.model_copy(deep=True)

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

    async def requeue_expired_task_leases(self) -> list[PlatformTaskRecord]:
        cutoff = datetime.now(timezone.utc).isoformat()
        records = [
            PlatformTaskRecord.model_validate(item)
            for item in await self.storage.requeue_expired_platform_task_leases(cutoff=cutoff)
        ]
        async with self._lock:
            for record in records:
                self._tasks[record.id] = record
        for record in records:
            await self._emit(record, "platform_harness.queue.task_requeued", {"reason": "worker_lease_expired"})
        return [record.model_copy(deep=True) for record in records]

    async def queue_semantics_snapshot(self, *, limit: int = 100) -> dict[str, Any]:
        await self.requeue_expired_task_leases()
        tasks = await self.list_tasks(limit=max(1, min(limit, 500)))
        heartbeats = await self.list_worker_heartbeats(limit=max(1, min(limit, 500)))
        counts = {
            "queued": 0,
            "running": 0,
            "paused": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 0,
        }
        leased_workers: dict[str, int] = {}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
            if task.worker_id:
                leased_workers[task.worker_id] = leased_workers.get(task.worker_id, 0) + 1
        return {
            "version": "v0.2.128",
            "source": "docs/archive/stage-report-archives/v0.2.x/v0.2.127_e08_remaining_sidecar_architecture_reselection.md",
            "queue_mode": "storage_backed_claim_next_with_requeue",
            "claim_next_atomic": True,
            "expired_lease_requeue": True,
            "task_counts": counts,
            "active_task_count": counts.get("queued", 0) + counts.get("running", 0),
            "leased_workers": leased_workers,
            "active_workers": [
                row.worker_id for row in heartbeats if row.liveness == "active"
            ],
            "stale_workers": [
                row.worker_id for row in heartbeats if row.liveness == "stale"
            ],
            "boundaries": {
                "external_process_manager": False,
                "external_kms_provider_integration": False,
                "full_sidecar_completion_claimed": False,
            },
        }

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

    async def record_worker_heartbeat(
        self,
        *,
        worker_id: str | None = None,
        status: WorkerHeartbeatStatus = "idle",
        active_task_id: str = "",
        stale_after_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PlatformWorkerHeartbeatRecord:
        effective_worker_id = self._effective_worker_id(worker_id)
        if not effective_worker_id.strip():
            raise PlatformHarnessViolation("worker heartbeat requires a non-empty worker_id")
        effective_stale_after = float(stale_after_seconds or max(self.worker_lease_seconds * 2, 120.0))
        if effective_stale_after <= 0:
            raise PlatformHarnessViolation("worker heartbeat stale_after_seconds must be greater than 0")
        record = PlatformWorkerHeartbeatRecord(
            worker_id=effective_worker_id,
            status=status,
            active_task_id=active_task_id,
            stale_after_seconds=effective_stale_after,
            metadata=metadata or {},
        )
        stored = await self.storage.save_platform_worker_heartbeat(record.model_dump(mode="json"))
        hydrated = self._worker_heartbeat_with_liveness(PlatformWorkerHeartbeatRecord.model_validate(stored))
        await self.storage.append_event(
            "platform_harness",
            "platform_harness.worker.heartbeat",
            hydrated.model_dump(mode="json"),
        )
        return hydrated

    async def list_worker_heartbeats(self, *, limit: int = 100) -> list[PlatformWorkerHeartbeatRecord]:
        rows = await self.storage.list_platform_worker_heartbeats(limit=limit)
        records = [
            self._worker_heartbeat_with_liveness(PlatformWorkerHeartbeatRecord.model_validate(row))
            for row in rows
        ]
        records.sort(key=lambda item: item.last_seen_at, reverse=True)
        return [record.model_copy(deep=True) for record in records]

    def _worker_heartbeat_with_liveness(
        self,
        record: PlatformWorkerHeartbeatRecord,
    ) -> PlatformWorkerHeartbeatRecord:
        seen_at = self._parse_datetime(record.last_seen_at)
        liveness = "stale"
        if seen_at is not None:
            stale_at = seen_at + timedelta(seconds=record.stale_after_seconds)
            if stale_at > datetime.now(timezone.utc):
                liveness = "active"
        return record.model_copy(update={"liveness": liveness})

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
        # 预算按当前工作段增量计（累计 - 复活基线）：返修 N 段的长寿命
        # 任务每段都有完整预算，而不是被历史段吃光后当场处决。
        baseline = record.usage_baseline or {}

        def segment(name: str) -> int:
            return record.usage_counts.get(name, 0) - baseline.get(name, 0)

        if usage_type == "model_call" and segment("model_call") > self.max_model_calls_per_task:
            return (
                "model call budget exceeded: "
                f"{segment('model_call')} > {self.max_model_calls_per_task}"
            )
        if usage_type == "tool_call" and segment("tool_call") > self.max_tool_calls_per_task:
            return (
                "tool call budget exceeded: "
                f"{segment('tool_call')} > {self.max_tool_calls_per_task}"
            )
        if (
            usage_type == "node_execution"
            and segment("node_execution") > self.max_node_executions_per_task
        ):
            return (
                "node execution budget exceeded: "
                f"{segment('node_execution')} > {self.max_node_executions_per_task}"
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
            f"secret policy blocked {surface}: forbidden secret field at {path}. "
            "裸凭证不能写进工作流配置：请让平台方把凭证存入密钥库（密钥值包含完整前缀，"
            "如 'Bearer xxx'），然后在这里用 {\"$secret\": \"密钥名\"} 引用它。"
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

    def contains_secret_reference(self, payload: Any) -> bool:
        if self.is_secret_reference(payload):
            return True
        if isinstance(payload, dict):
            return any(self.contains_secret_reference(value) for value in payload.values())
        if isinstance(payload, list):
            return any(self.contains_secret_reference(value) for value in payload)
        return False

    async def inject_secret_references(
        self,
        *,
        owner_id: str,
        payload: Any,
        allow_secret_references: bool = True,
    ) -> Any:
        if self.is_secret_reference(payload):
            if not allow_secret_references:
                raise PlatformHarnessViolation(
                    "platform secret references are outside this execution policy"
                )
            return await self._resolve_secret_reference(owner_id=owner_id, reference=payload)
        if isinstance(payload, dict):
            return {
                key: await self.inject_secret_references(
                    owner_id=owner_id,
                    payload=value,
                    allow_secret_references=allow_secret_references,
                )
                for key, value in payload.items()
            }
        if isinstance(payload, list):
            return [
                await self.inject_secret_references(
                    owner_id=owner_id,
                    payload=value,
                    allow_secret_references=allow_secret_references,
                )
                for value in payload
            ]
        return payload

    async def _resolve_secret_reference(self, *, owner_id: str, reference: dict[str, Any]) -> str:
        raw_ref = next((reference.get(key) for key in SECRET_REFERENCE_KEYS if reference.get(key)), "")
        ref_owner, name = self._split_secret_reference(str(raw_ref), owner_id)
        if reference.get("owner_id"):
            ref_owner = str(reference["owner_id"])
        if ref_owner != owner_id:
            raise PlatformHarnessViolation(
                "platform secret reference owner does not match the execution owner"
            )
        self._validate_secret_identity(ref_owner, name)
        try:
            row = await self.storage.get_platform_secret(owner_id=ref_owner, name=name)
        except KeyError as error:
            raise PlatformHarnessViolation(str(error)) from error
        prefix = str(reference.get("prefix", ""))
        suffix = str(reference.get("suffix", ""))
        return f"{prefix}{self._decrypt_secret_value(str(row['value']))}{suffix}"

    def _encrypt_secret_value(self, value: str) -> str:
        if self.secret_kms_provider is not None:
            return self._encrypt_secret_value_v3(value)
        if not self.secret_envelope_key:
            return value
        salt = os.urandom(16)
        nonce = os.urandom(16)
        enc_key, mac_key = self._derive_secret_envelope_keys(self.secret_envelope_key, salt)
        plaintext = value.encode("utf-8")
        ciphertext = self._xor_bytes(plaintext, self._keystream(enc_key, nonce, len(plaintext)))
        envelope = {
            "algorithm": "hmac-sha256-xor-stream",
            "ciphertext": self._b64(ciphertext),
            "iterations": SECRET_ENVELOPE_ITERATIONS,
            "key_id": self.secret_envelope_key_id,
            "kdf": "pbkdf2-hmac-sha256",
            "nonce": self._b64(nonce),
            "salt": self._b64(salt),
            "version": 2,
        }
        mac_input = self._stable_json(envelope).encode("utf-8")
        envelope["tag"] = self._b64(hmac.new(mac_key, mac_input, hashlib.sha256).digest())
        return SECRET_ENVELOPE_V2_PREFIX + self._b64(self._stable_json(envelope).encode("utf-8"))

    def _encrypt_secret_value_v3(self, value: str) -> str:
        if self.secret_kms_provider is None:
            raise PlatformHarnessViolation("platform secret KMS provider is not configured")
        data_key = os.urandom(32)
        salt = os.urandom(16)
        nonce = os.urandom(16)
        enc_key, mac_key = self._derive_secret_envelope_keys(self._b64(data_key), salt)
        plaintext = value.encode("utf-8")
        ciphertext = self._xor_bytes(plaintext, self._keystream(enc_key, nonce, len(plaintext)))
        envelope = {
            "algorithm": "hmac-sha256-xor-stream",
            "ciphertext": self._b64(ciphertext),
            "iterations": SECRET_ENVELOPE_ITERATIONS,
            "key_id": self.secret_kms_provider.primary_key_id,
            "kdf": "pbkdf2-hmac-sha256",
            "nonce": self._b64(nonce),
            "provider_id": self.secret_kms_provider.provider_id,
            "salt": self._b64(salt),
            "version": 3,
            "wrapped_data_key": self.secret_kms_provider.wrap_data_key(data_key),
        }
        mac_input = self._stable_json(envelope).encode("utf-8")
        envelope["tag"] = self._b64(hmac.new(mac_key, mac_input, hashlib.sha256).digest())
        return SECRET_ENVELOPE_V3_PREFIX + self._b64(self._stable_json(envelope).encode("utf-8"))

    def _decrypt_secret_value(self, stored_value: str) -> str:
        if not self._is_encrypted_secret_value(stored_value):
            return stored_value
        if stored_value.startswith(SECRET_ENVELOPE_V3_PREFIX):
            return self._decrypt_secret_value_v3(stored_value)
        if not self.secret_envelope_keyring:
            raise PlatformHarnessViolation("platform secret envelope key is not configured")
        prefix = SECRET_ENVELOPE_V2_PREFIX if stored_value.startswith(SECRET_ENVELOPE_V2_PREFIX) else SECRET_ENVELOPE_PREFIX
        try:
            raw = self._unb64(stored_value.removeprefix(prefix))
            envelope = json.loads(raw.decode("utf-8"))
            tag = self._unb64(str(envelope.pop("tag")))
            salt = self._unb64(str(envelope["salt"]))
            nonce = self._unb64(str(envelope["nonce"]))
            ciphertext = self._unb64(str(envelope["ciphertext"]))
        except Exception as error:
            raise PlatformHarnessViolation("platform secret envelope is invalid") from error
        key_id = str(envelope.get("key_id") or self.secret_envelope_key_id)
        envelope_key = self.secret_envelope_keyring.get(key_id)
        if not envelope_key:
            raise PlatformHarnessViolation(f"platform secret envelope key is not configured: {key_id}")
        enc_key, mac_key = self._derive_secret_envelope_keys(envelope_key, salt)
        mac_input = self._stable_json(envelope).encode("utf-8")
        expected = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise PlatformHarnessViolation("platform secret envelope authentication failed")
        plaintext = self._xor_bytes(ciphertext, self._keystream(enc_key, nonce, len(ciphertext)))
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PlatformHarnessViolation("platform secret envelope plaintext is invalid") from error

    def _decrypt_secret_value_v3(self, stored_value: str) -> str:
        if self.secret_kms_provider is None:
            raise PlatformHarnessViolation("platform secret KMS provider is not configured")
        try:
            raw = self._unb64(stored_value.removeprefix(SECRET_ENVELOPE_V3_PREFIX))
            envelope = json.loads(raw.decode("utf-8"))
            tag = self._unb64(str(envelope.pop("tag")))
            salt = self._unb64(str(envelope["salt"]))
            nonce = self._unb64(str(envelope["nonce"]))
            ciphertext = self._unb64(str(envelope["ciphertext"]))
            wrapped_data_key = dict(envelope["wrapped_data_key"])
        except Exception as error:
            raise PlatformHarnessViolation("platform secret KMS envelope is invalid") from error
        provider_id = str(envelope.get("provider_id") or "")
        if provider_id != self.secret_kms_provider.provider_id:
            raise PlatformHarnessViolation(f"platform secret KMS provider is not configured: {provider_id}")
        try:
            data_key = self.secret_kms_provider.unwrap_data_key(wrapped_data_key)
        except ValueError as error:
            raise PlatformHarnessViolation(str(error)) from error
        enc_key, mac_key = self._derive_secret_envelope_keys(self._b64(data_key), salt)
        mac_input = self._stable_json(envelope).encode("utf-8")
        expected = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise PlatformHarnessViolation("platform secret KMS envelope authentication failed")
        plaintext = self._xor_bytes(ciphertext, self._keystream(enc_key, nonce, len(ciphertext)))
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PlatformHarnessViolation("platform secret KMS envelope plaintext is invalid") from error

    def _derive_secret_envelope_keys(self, envelope_key: str, salt: bytes) -> tuple[bytes, bytes]:
        material = hashlib.pbkdf2_hmac(
            "sha256",
            envelope_key.encode("utf-8"),
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
        if stored_value.startswith(SECRET_ENVELOPE_V3_PREFIX):
            provider_id, key_id = self._secret_envelope_v3_provider_and_key_id(stored_value)
            if provider_id and key_id:
                return f"encrypted_v3:{provider_id}:{key_id}"
            return "encrypted_v3:unknown"
        if stored_value.startswith(SECRET_ENVELOPE_V2_PREFIX):
            key_id = self._secret_envelope_key_id(stored_value)
            return f"encrypted_v2:{key_id}" if key_id else "encrypted_v2:unknown"
        if stored_value.startswith(SECRET_ENVELOPE_PREFIX):
            return "encrypted_v1"
        return "legacy_plaintext"

    def _secret_envelope_key_id(self, stored_value: str) -> str:
        if not stored_value.startswith(SECRET_ENVELOPE_V2_PREFIX):
            return ""
        try:
            raw = self._unb64(stored_value.removeprefix(SECRET_ENVELOPE_V2_PREFIX))
            envelope = json.loads(raw.decode("utf-8"))
        except Exception:
            return ""
        return str(envelope.get("key_id") or "")

    def _secret_envelope_v3_provider_and_key_id(self, stored_value: str) -> tuple[str, str]:
        if not stored_value.startswith(SECRET_ENVELOPE_V3_PREFIX):
            return "", ""
        try:
            raw = self._unb64(stored_value.removeprefix(SECRET_ENVELOPE_V3_PREFIX))
            envelope = json.loads(raw.decode("utf-8"))
        except Exception:
            return "", ""
        return str(envelope.get("provider_id") or ""), str(envelope.get("key_id") or "")

    def _is_encrypted_secret_value(self, stored_value: str) -> bool:
        return (
            stored_value.startswith(SECRET_ENVELOPE_PREFIX)
            or stored_value.startswith(SECRET_ENVELOPE_V2_PREFIX)
            or stored_value.startswith(SECRET_ENVELOPE_V3_PREFIX)
        )

    def _normalized_secret_key_id(self, key_id: str) -> str:
        normalized = key_id.strip() or "local"
        if "/" in normalized or ":" in normalized:
            raise PlatformHarnessViolation("platform secret envelope key id must not contain / or :")
        return normalized

    def _secret_envelope_keyring(
        self,
        *,
        current_key_id: str,
        current_key: str,
        previous_keys: dict[str, str],
    ) -> dict[str, str]:
        keyring: dict[str, str] = {}
        for key_id, key in previous_keys.items():
            normalized_id = self._normalized_secret_key_id(str(key_id))
            if key:
                keyring[normalized_id] = str(key)
        if current_key:
            keyring[current_key_id] = current_key
        return keyring

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
        key_id = ""
        provider_id = ""
        if storage_mode.startswith("encrypted_v3:"):
            _, provider_id, key_id = storage_mode.split(":", 2)
        elif storage_mode.startswith("encrypted_v2:"):
            key_id = storage_mode.split(":", 1)[1]
        return {
            "id": row["id"],
            "owner_id": row["owner_id"],
            "name": row["name"],
            "description": row.get("description", ""),
            "secret_ref": f"secret://{row['owner_id']}/{row['name']}",
            "storage_mode": storage_mode,
            "encrypted": storage_mode.startswith("encrypted"),
            "key_id": key_id,
            "kms_provider_id": provider_id,
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
        declared_egress_hosts: list[str] | None = None,
        agent_network_allowlist: list[str] | None = None,
    ) -> None:
        decision = self.explain_stdio_mcp_policy(
            surface=surface,
            server_name=server_name,
            agent_network_policy=agent_network_policy,
            sandbox_network_policy=sandbox_network_policy,
            declared_egress_hosts=declared_egress_hosts,
            agent_network_allowlist=agent_network_allowlist,
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
        declared_egress_hosts: list[str] | None = None,
        agent_network_allowlist: list[str] | None = None,
    ) -> dict[str, Any]:
        platform_policy = self._normalized_policy(self.network_egress_policy)
        agent_policy = self._normalized_policy(agent_network_policy)
        sandbox_policy = (
            self._normalized_policy(sandbox_network_policy)
            if sandbox_network_policy is not None
            else None
        )
        normalized_declared_hosts = self._normalized_host_list(declared_egress_hosts or [])
        normalized_agent_allowlist = self._normalized_host_list(agent_network_allowlist or [])
        normalized_platform_allowlist = self._normalized_host_list(self.network_egress_allowlist)
        base = {
            "surface": surface,
            "server_name": server_name,
            "platform_policy": platform_policy,
            "agent_network_policy": agent_policy,
            "sandbox_network_policy": sandbox_policy,
            "declared_egress_hosts": normalized_declared_hosts,
            "agent_network_allowlist": normalized_agent_allowlist,
            "platform_network_allowlist": normalized_platform_allowlist,
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
            if sandbox_policy != "allowlist" or agent_policy != "allowlist":
                reason = (
                    "stdio MCP egress policy blocked "
                    f"{surface}:{server_name}: allowlist-grade stdio requires sandboxed execution "
                    "with agent and sandbox allowlist policies"
                )
                action = "Run stdio MCP inside the sandbox runner with declared egress_hosts and allowlist policy."
                return {**base, "reason": reason, "operator_action": action}
            if not normalized_declared_hosts:
                reason = (
                    "stdio MCP egress policy blocked "
                    f"{surface}:{server_name}: allowlist-grade stdio requires declared egress_hosts"
                )
                action = "Add egress_hosts to the stdio MCP server spec or use sandboxed no-network mode."
                return {**base, "reason": reason, "operator_action": action}
            agent_denied = [
                host for host in normalized_declared_hosts
                if not self._host_in_allowlist(host, normalized_agent_allowlist)
            ]
            if agent_denied:
                denied = ", ".join(agent_denied)
                reason = (
                    "stdio MCP egress policy blocked "
                    f"{surface}:{server_name}: declared host(s) {denied} are not in the agent allowlist"
                )
                action = "Add the declared host to the agent network_allowlist or remove it from egress_hosts."
                return {**base, "reason": reason, "operator_action": action}
            if platform_policy == "allowlist":
                platform_denied = [
                    host for host in normalized_declared_hosts
                    if not self._host_in_allowlist(host, normalized_platform_allowlist)
                ]
                if platform_denied:
                    denied = ", ".join(platform_denied)
                    reason = (
                        "stdio MCP egress policy blocked "
                        f"{surface}:{server_name}: declared host(s) {denied} are not in the platform allowlist"
                    )
                    action = "Add the declared host to the Platform Harness network egress allowlist."
                    return {**base, "reason": reason, "operator_action": action}
            return {
                **base,
                "allowed": True,
                "mode": "sandboxed_allowlist",
                "reason": (
                    f"stdio MCP allowed {surface}:{server_name}: sandboxed stdio declares egress_hosts "
                    "covered by the active allowlist policy"
                ),
                "operator_action": "Keep declared egress_hosts aligned with the sandbox/container firewall allowlist.",
            }
        else:
            reason = (
                "stdio MCP egress policy blocked "
                f"{surface}:{server_name}: stdio servers do not declare hostnames, "
                "so allowlist-grade enforcement requires hard sandbox/container firewalling"
            )
            action = (
                "Use an HTTP MCP server with a hostname allowlist, switch to a no-network "
                "sandbox for local stdio, or add hard sandbox firewalling before enabling stdio allowlist."
            )
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
                declared_egress_hosts=list(self.network_egress_allowlist),
                agent_network_allowlist=list(self.network_egress_allowlist),
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
                "new_secret_mode": self._new_secret_storage_mode(),
                "envelope_configured": bool(self.secret_envelope_key) or self.secret_kms_provider is not None,
                "current_key_id": self._current_secret_key_id(),
                "keyring_size": len(self.secret_envelope_keyring),
                "rotation_aware": bool(self.secret_envelope_keyring),
                "kms_provider_configured": self.secret_kms_provider is not None,
                "kms_provider": self.secret_kms_provider.status()
                if self.secret_kms_provider is not None
                else {
                    "provider_id": "",
                    "provider_type": "",
                    "configured": False,
                    "primary_key_id": "",
                    "keyring_size": 0,
                    "rotation_aware": False,
                    "wrap_supported": False,
                    "unwrap_supported": False,
                },
                "external_kms_provider_integration": self.secret_kms_provider is not None,
                "legacy_v1_read_supported": True,
                "legacy_plaintext_read_supported": True,
            },
            "cancellation_policy": self.cancellation_policy,
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
                "allowlist_supported": True,
                "allowlist_contract": {
                    "requires_sandboxed_stdio": True,
                    "requires_declared_egress_hosts": True,
                    "requires_agent_allowlist_coverage": True,
                    "requires_platform_allowlist_coverage_when_platform_policy_is_allowlist": True,
                },
                "decisions": decisions,
            },
            "e08_boundary": self._e08_boundary_summary(),
        }

    def update_policy_controls(
        self,
        *,
        network_egress_policy: str | None = None,
        network_egress_allowlist: list[str] | None = None,
        cancellation_policy: str | None = None,
        secret_policy_enabled: bool | None = None,
        worker_lease_seconds: float | None = None,
        limits: dict[str, int] | None = None,
        reason: str,
    ) -> dict[str, Any]:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise PlatformHarnessViolation("policy controls update requires a non-empty reason")

        before = self.policy_controls()
        changed_fields: list[str] = []

        if network_egress_policy is not None:
            normalized_policy = self._normalized_policy(network_egress_policy)
            if normalized_policy not in {"full", "allowlist", "none"}:
                raise PlatformHarnessViolation(
                    "network_egress_policy must be one of: full, allowlist, none"
                )
            if normalized_policy != self._normalized_policy(self.network_egress_policy):
                self.network_egress_policy = normalized_policy
                changed_fields.append("network_egress_policy")

        if network_egress_allowlist is not None:
            normalized_allowlist = self._normalized_allowlist(network_egress_allowlist)
            if normalized_allowlist != self.network_egress_allowlist:
                self.network_egress_allowlist = normalized_allowlist
                changed_fields.append("network_egress_allowlist")

        if cancellation_policy is not None:
            normalized_cancellation = self._normalized_cancellation_policy(cancellation_policy)
            if normalized_cancellation != self.cancellation_policy:
                self.cancellation_policy = normalized_cancellation
                changed_fields.append("cancellation_policy")

        if secret_policy_enabled is not None and secret_policy_enabled != self.secret_policy_enabled:
            self.secret_policy_enabled = secret_policy_enabled
            changed_fields.append("secret_policy_enabled")

        if worker_lease_seconds is not None:
            if worker_lease_seconds < 0:
                raise PlatformHarnessViolation("worker_lease_seconds must be non-negative")
            normalized_lease = float(worker_lease_seconds)
            if normalized_lease != self.worker_lease_seconds:
                self.worker_lease_seconds = normalized_lease
                changed_fields.append("worker_lease_seconds")

        if limits is not None:
            changed_fields.extend(self._update_policy_limits(limits))

        if not changed_fields:
            raise PlatformHarnessViolation("policy controls update did not change any fields")

        after = self.policy_controls()
        return {
            "before": before,
            "after": after,
            "audit": {
                "version": "v0.2.96",
                "action": "platform_harness.policy_controls.updated",
                "reason": normalized_reason,
                "changed_fields": changed_fields,
                "not_persistent_across_restart": True,
                "not_full_sidecar_completion": True,
            },
        }

    def _new_secret_storage_mode(self) -> str:
        if self.secret_kms_provider is not None:
            return f"encrypted_v3:{self.secret_kms_provider.provider_id}:{self.secret_kms_provider.primary_key_id}"
        if self.secret_envelope_key:
            return f"encrypted_v2:{self.secret_envelope_key_id}"
        return "legacy_plaintext"

    def _current_secret_key_id(self) -> str:
        if self.secret_kms_provider is not None:
            return self.secret_kms_provider.primary_key_id
        if self.secret_envelope_key:
            return self.secret_envelope_key_id
        return ""

    def _normalized_allowlist(self, entries: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            value = self._normalized_host(entry)
            if not value:
                raise PlatformHarnessViolation("network_egress_allowlist entries must be non-empty")
            if value not in seen:
                normalized.append(value)
                seen.add(value)
        return normalized

    def _normalized_host(self, value: str) -> str:
        normalized = value.strip().casefold().rstrip(".")
        if normalized and ("/" in normalized or ":" in normalized):
            raise PlatformHarnessViolation("network_egress_allowlist entries must be host names")
        return normalized

    def _normalized_host_list(self, entries: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            value = self._normalized_host(str(entry))
            if value and value not in seen:
                normalized.append(value)
                seen.add(value)
        return normalized

    def _host_in_allowlist(self, hostname: str, allowlist: list[str]) -> bool:
        normalized = self._normalized_host(hostname)
        return any(normalized == entry or normalized.endswith(f".{entry}") for entry in allowlist)

    def _normalized_cancellation_policy(self, policy: str) -> str:
        normalized = policy.strip().casefold()
        if normalized not in {"enabled", "disabled"}:
            raise PlatformHarnessViolation("cancellation_policy must be one of: enabled, disabled")
        return normalized

    def enforce_cancellation_policy(self) -> None:
        if self.cancellation_policy == "disabled":
            raise PlatformHarnessViolation("workflow cancellation is disabled by Platform Harness policy")

    def _update_policy_limits(self, limits: dict[str, int]) -> list[str]:
        allowed = {
            "max_active_tasks",
            "max_model_calls_per_task",
            "max_tool_calls_per_task",
            "max_node_executions_per_task",
            "max_model_calls_per_owner",
            "max_tool_calls_per_owner",
            "max_node_executions_per_owner",
        }
        changed: list[str] = []
        for key, value in limits.items():
            if key not in allowed:
                raise PlatformHarnessViolation(f"unknown policy limit: {key}")
            if not isinstance(value, int) or value < 0:
                raise PlatformHarnessViolation(f"{key} must be a non-negative integer")
            if getattr(self, key) != value:
                setattr(self, key, value)
                changed.append(f"limits.{key}")
        return changed

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
            "source": "docs/archive/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            "comparison_evidence": "docs/archive/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10_summary.md",
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
                    "id": "cancellation_policy",
                    "label": "Cancellation policy",
                    "layer": "platform_harness",
                    "status": self.cancellation_policy,
                    "value": self.cancellation_policy,
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
                "source": "docs/archive/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            },
            {
                "id": "cancellation_checkpoint",
                "layer": "workflow_runtime",
                "enforcement": "soft_checkpoint",
                "status": self.cancellation_policy,
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
        declared_egress_hosts: list[str] | None = None,
        agent_network_allowlist: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": decision_id,
            "label": label,
            **self.explain_stdio_mcp_policy(
                surface="policy_controls",
                server_name=decision_id,
                agent_network_policy=agent_policy,
                sandbox_network_policy=sandbox_policy,
                declared_egress_hosts=declared_egress_hosts,
                agent_network_allowlist=agent_network_allowlist,
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
