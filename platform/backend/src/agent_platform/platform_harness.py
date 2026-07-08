from __future__ import annotations

import asyncio
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
    ) -> None:
        self.storage = storage
        self.max_active_tasks = max_active_tasks
        self.max_model_calls_per_task = max_model_calls_per_task
        self.max_tool_calls_per_task = max_tool_calls_per_task
        self.max_node_executions_per_task = max_node_executions_per_task
        self.max_model_calls_per_owner = max_model_calls_per_owner
        self.max_tool_calls_per_owner = max_tool_calls_per_owner
        self.max_node_executions_per_owner = max_node_executions_per_owner
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
    ) -> PlatformTaskRecord:
        existing = await self._cached_or_persisted_task(task_id)
        if existing:
            should_emit = False
            async with self._lock:
                record = self._tasks[task_id]
                if record.status == "paused":
                    record.status = "running"
                    record.updated_at = utc_now()
                    record.finished_at = None
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
            record.status = status
            record.error = error
            record.updated_at = utc_now()
            record.finished_at = record.updated_at
            if metadata:
                record.metadata.update(metadata)
        await self._persist(record)
        await self._emit(record, f"platform_harness.task.{status}")
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
