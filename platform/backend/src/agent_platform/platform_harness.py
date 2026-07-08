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
    """In-process Platform Harness task monitor.

    This is intentionally small: it gives Lilies a hard platform-side task
    boundary and resource counters without pretending to be a durable queue.
    Every transition is also emitted to the event store for audit.
    """

    def __init__(
        self,
        *,
        storage: Storage,
        max_active_tasks: int = 100,
        max_model_calls_per_task: int = 100,
        max_tool_calls_per_task: int = 200,
        max_node_executions_per_task: int = 1000,
    ) -> None:
        self.storage = storage
        self.max_active_tasks = max_active_tasks
        self.max_model_calls_per_task = max_model_calls_per_task
        self.max_tool_calls_per_task = max_tool_calls_per_task
        self.max_node_executions_per_task = max_node_executions_per_task
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
        async with self._lock:
            existing = self._tasks.get(task_id)
            if existing:
                if existing.status == "paused":
                    existing.status = "running"
                    existing.updated_at = utc_now()
                    existing.finished_at = None
                return existing
            active = sum(1 for item in self._tasks.values() if item.status in {"queued", "running"})
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
            violation = self._violation(record, usage_type)
            if violation:
                record.status = "failed"
                record.error = violation
                record.finished_at = record.updated_at
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
        await self._emit(record, f"platform_harness.task.{status}")
        return record

    async def get_task(self, task_id: str) -> PlatformTaskRecord:
        async with self._lock:
            try:
                return self._tasks[task_id].model_copy(deep=True)
            except KeyError:
                raise KeyError(f"platform task not found: {task_id}") from None

    async def list_tasks(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        owner_id: str | None = None,
        limit: int = 100,
    ) -> list[PlatformTaskRecord]:
        async with self._lock:
            tasks = list(self._tasks.values())
        if kind:
            tasks = [item for item in tasks if item.kind == kind]
        if status:
            tasks = [item for item in tasks if item.status == status]
        if owner_id:
            tasks = [item for item in tasks if item.owner_id == owner_id]
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

    async def _emit(
        self, record: PlatformTaskRecord, event_type: str, extra: dict[str, Any] | None = None
    ) -> None:
        data = {
            "task": record.model_dump(mode="json"),
            **(extra or {}),
        }
        await self.storage.append_event("platform_harness", event_type, data)
        await self.storage.append_event(record.owner_id, event_type, data)
