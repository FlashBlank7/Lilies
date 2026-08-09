"""Governed background tasks with state machine, cancel, timeout, and audit.

Provides GovernedTask — a wrapper around asyncio.create_task that enforces
the platform harness principle: every long-running background operation must
have a task ID, state machine, budget, cancel support, timeout, and audit events.

This replaces bare ``asyncio.create_task()`` for Platform Harness operations,
closing the governance gap identified in:

  docs/intellectual-assets/asset_platform_harness_task_monitor_boundary.md

Usage::

    gov = GovernedTask(
        name="auto-evolve",
        max_timeout_seconds=300,
        emit=storage.append_event,
    )
    task = gov.run(stream_id, my_coroutine())
    # ... later ...
    gov.cancel()
    status = await gov.wait()
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Coroutine


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"


CLEANUP_STATUSES = frozenset({TaskStatus.completed, TaskStatus.failed, TaskStatus.cancelled, TaskStatus.timed_out})


@dataclass(slots=True)
class TaskRecord:
    """Observable record of one governed task execution."""

    task_id: str
    name: str
    status: TaskStatus = TaskStatus.queued
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    elapsed_ms: float | None = None
    error: str = ""
    error_type: str = ""
    budget_consumed: float = 0.0  # approximate cost in USD
    events: list[dict[str, Any]] = field(default_factory=list)


EmitFunc = Callable[[str, str, dict[str, Any]], Awaitable[None]]


class GovernedTask:
    """Wraps a coroutine in a governed asyncio task with lifecycle management.

    All state transitions emit structured events via *emit* for audit trail.
    """

    def __init__(
        self,
        *,
        name: str,
        max_timeout_seconds: float = 300,
        emit: EmitFunc | None = None,
    ) -> None:
        self.name = name
        self.max_timeout_seconds = max_timeout_seconds
        self._emit = emit
        self._task: asyncio.Task[Any] | None = None
        self._records: dict[str, TaskRecord] = {}
        self._lock = asyncio.Lock()

    # ── public API ────────────────────────────────────────────────────

    def run(
        self,
        stream_id: str,
        coro: Coroutine[Any, Any, Any],
        *,
        task_id: str | None = None,
    ) -> asyncio.Task[Any]:
        """Launch *coro* as a governed task.

        Returns the underlying asyncio.Task so the caller can attach
        done callbacks or cancel via asyncio.
        """
        from uuid import uuid4
        task_id = task_id or str(uuid4())

        record = TaskRecord(task_id=task_id, name=self.name)
        self._records[task_id] = record
        record.status = TaskStatus.queued

        async def _wrapped() -> Any:
            async with self._lock:
                record.status = TaskStatus.running
                record.started_at = time.monotonic()
            await self._emit_event(stream_id, "governed_task.started", {
                "task_id": task_id,
                "name": self.name,
                "max_timeout_seconds": self.max_timeout_seconds,
            })
            try:
                result = await asyncio.wait_for(coro, timeout=self.max_timeout_seconds)
                async with self._lock:
                    record.status = TaskStatus.completed
                    record.finished_at = time.monotonic()
                    record.elapsed_ms = (record.finished_at - (record.started_at or record.created_at)) * 1000
                await self._emit_event(stream_id, "governed_task.completed", {
                    "task_id": task_id,
                    "name": self.name,
                    "elapsed_ms": record.elapsed_ms,
                })
                return result
            except asyncio.TimeoutError:
                async with self._lock:
                    record.status = TaskStatus.timed_out
                    record.error = f"timed out after {self.max_timeout_seconds}s"
                    record.finished_at = time.monotonic()
                    record.elapsed_ms = (record.finished_at - (record.started_at or record.created_at)) * 1000
                await self._emit_event(stream_id, "governed_task.timed_out", {
                    "task_id": task_id,
                    "name": self.name,
                    "timeout_seconds": self.max_timeout_seconds,
                })
                raise
            except asyncio.CancelledError:
                async with self._lock:
                    record.status = TaskStatus.cancelled
                    record.finished_at = time.monotonic()
                    record.elapsed_ms = (record.finished_at - (record.started_at or record.created_at)) * 1000
                await self._emit_event(stream_id, "governed_task.cancelled", {
                    "task_id": task_id,
                    "name": self.name,
                })
                raise
            except Exception as exc:
                async with self._lock:
                    record.status = TaskStatus.failed
                    record.error = str(exc)
                    record.error_type = type(exc).__name__
                    record.finished_at = time.monotonic()
                    record.elapsed_ms = (record.finished_at - (record.started_at or record.created_at)) * 1000
                await self._emit_event(stream_id, "governed_task.failed", {
                    "task_id": task_id,
                    "name": self.name,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                })
                raise

        self._task = asyncio.create_task(_wrapped())
        return self._task

    async def cancel(self, stream_id: str = "") -> None:
        """Request cancellation of the governed task."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if stream_id:
            await self._emit_event(stream_id, "governed_task.cancel_requested", {
                "name": self.name,
            })

    async def wait(self, task_id: str) -> TaskRecord:
        """Block until the task finishes, then return its record."""
        if self._task:
            try:
                await self._task
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        return self._records.get(task_id, TaskRecord(task_id=task_id, name=self.name))

    @property
    def status(self) -> TaskStatus | None:
        """Current status of the underlying asyncio task."""
        if self._task is None:
            return None
        if self._task.done():
            if self._task.cancelled():
                return TaskStatus.cancelled
            exc = self._task.exception()
            if exc is None:
                return TaskStatus.completed
            if isinstance(exc, asyncio.TimeoutError):
                return TaskStatus.timed_out
            return TaskStatus.failed
        return TaskStatus.running

    def records(self) -> dict[str, TaskRecord]:
        """Snapshot of all task records."""
        return dict(self._records)

    # ── internal ──────────────────────────────────────────────────────

    async def _emit_event(self, stream_id: str, event_type: str, data: dict[str, Any]) -> None:
        if self._emit:
            try:
                await self._emit(stream_id, event_type, data)
            except Exception:
                pass  # audit failure must not crash the task
