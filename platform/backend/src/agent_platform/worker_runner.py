from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .platform_harness import PlatformHarness, PlatformTaskRecord


PlatformTaskHandler = Callable[[PlatformTaskRecord], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


@dataclass(slots=True)
class WorkerRunResult:
    task_id: str
    kind: str
    status: str
    worker_id: str
    lease_version: int = 0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class PlatformHarnessWorkerRunner:
    """Lease-consuming worker runner for queued Platform Harness tasks.

    This is a narrow primitive: callers provide handlers for task kinds they
    know how to execute. Unsupported tasks are left queued and unclaimed.
    """

    def __init__(
        self,
        *,
        harness: PlatformHarness,
        worker_id: str,
        lease_seconds: float = 60.0,
        handlers: dict[str, PlatformTaskHandler] | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than 0")
        self.harness = harness
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.handlers = handlers or {}

    def register_handler(self, kind: str, handler: PlatformTaskHandler) -> None:
        self.handlers[kind] = handler

    async def run_once(
        self,
        *,
        kind: str | None = None,
        owner_id: str | None = None,
        limit: int = 10,
    ) -> list[WorkerRunResult]:
        tasks = await self.harness.list_tasks(
            kind=kind,
            status="queued",
            owner_id=owner_id,
            limit=max(1, limit),
        )
        results: list[WorkerRunResult] = []
        for task in tasks:
            handler = self.handlers.get(task.kind)
            if handler is None:
                results.append(self._result(task, "skipped", error=f"no handler for kind: {task.kind}"))
                continue
            try:
                claimed = await self.harness.claim_task_lease(
                    task.id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as error:
                results.append(self._result(task, "claim_failed", error=str(error)))
                continue
            try:
                output = handler(claimed)
                if inspect.isawaitable(output):
                    output = await output
                result_metadata = output or {}
            except Exception as error:
                metadata = self._worker_metadata(status="failed", result={})
                finished = await self.harness.finish_task(
                    claimed.id,
                    status="failed",
                    error=str(error),
                    metadata=metadata,
                )
                results.append(self._result(finished or claimed, "failed", error=str(error), metadata=metadata))
                continue
            metadata = self._worker_metadata(status="succeeded", result=result_metadata)
            finished = await self.harness.finish_task(
                claimed.id,
                status="succeeded",
                metadata=metadata,
            )
            results.append(self._result(finished or claimed, "succeeded", metadata=metadata))
        return results

    def _worker_metadata(self, *, status: str, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "worker_runner": {
                "worker_id": self.worker_id,
                "status": status,
                "result": result,
            }
        }

    def _result(
        self,
        task: PlatformTaskRecord,
        status: str,
        *,
        error: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> WorkerRunResult:
        return WorkerRunResult(
            task_id=task.id,
            kind=task.kind,
            status=status,
            worker_id=self.worker_id,
            lease_version=task.lease_version,
            error=error,
            metadata=metadata or {},
        )
