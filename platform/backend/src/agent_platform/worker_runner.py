from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .platform_harness import PlatformHarness, PlatformTaskRecord, WorkerHeartbeatStatus


PLATFORM_WORKER_TASK_KINDS = (
    "workflow_run",
    "builder_build",
    "test_suite",
    "scheduler_trigger",
    "scheduler_manual_trigger",
    "benchmark",
    "draft_patch_preview",
)

IMPLEMENTED_WORKER_HANDLERS: dict[str, dict[str, str]] = {
    "scheduler_trigger": {
        "label": "Scheduler automatic trigger",
        "implementation": "scheduler_trigger_handler",
        "evidence": "docs/stage-reports/v0.2.114_e08_scheduler_trigger_worker_offload_handler.md",
    },
    "scheduler_manual_trigger": {
        "label": "Scheduler manual trigger",
        "implementation": "scheduler_manual_trigger_handler",
        "evidence": "docs/stage-reports/v0.2.27_worker_runner_cli_and_handler.md",
    },
}

UNAVAILABLE_WORKER_HANDLERS: dict[str, dict[str, str]] = {
    "workflow_run": {
        "label": "Workflow run",
        "reason": "workflow runs are currently managed by the API/runtime path, not the external worker catalog",
        "operator_action": "Keep workflow runs on the runtime path until a worker-owned workflow handler is implemented.",
    },
    "builder_build": {
        "label": "Builder build",
        "reason": "builder builds are currently managed by the builder service path",
        "operator_action": "Keep builder builds on the builder service path until a worker-owned build handler is implemented.",
    },
    "test_suite": {
        "label": "Test suite",
        "reason": "test suites are currently managed by the workflow runtime test path",
        "operator_action": "Keep test suites on the runtime test path until a worker-owned test handler is implemented.",
    },
    "benchmark": {
        "label": "Benchmark",
        "reason": "benchmark tasks are currently recorded by benchmark APIs and scripts, not executed by the worker catalog",
        "operator_action": "Keep benchmark execution on the benchmark path until a worker-owned benchmark handler is implemented.",
    },
    "draft_patch_preview": {
        "label": "Draft patch preview",
        "reason": "draft patch previews are currently managed by the API preview path",
        "operator_action": "Keep draft previews on the API path until a worker-owned preview handler is implemented.",
    },
}


PlatformTaskHandler = Callable[[PlatformTaskRecord], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


class PlatformWorkerHandlerUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class WorkerRunResult:
    task_id: str
    kind: str
    status: str
    worker_id: str
    lease_version: int = 0
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerHandlerCatalogEntry:
    kind: str
    label: str
    required: bool
    status: str
    handler_registered: bool
    executable: bool
    implementation: str
    evidence: str
    reason: str
    operator_action: str


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
        renewal_interval_seconds: float | None = None,
        handlers: dict[str, PlatformTaskHandler] | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than 0")
        if renewal_interval_seconds is not None and renewal_interval_seconds <= 0:
            raise ValueError("renewal_interval_seconds must be greater than 0")
        self.harness = harness
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.renewal_interval_seconds = renewal_interval_seconds or (lease_seconds / 2)
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
        await self._record_heartbeat(status="idle", metadata={"phase": "poll_start"})
        tasks = await self.harness.list_tasks(
            kind=kind,
            status="queued",
            owner_id=owner_id,
            limit=max(1, limit),
        )
        results: list[WorkerRunResult] = []
        if not tasks:
            await self._record_heartbeat(status="idle", metadata={"phase": "poll_empty"})
            return results
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
            await self._record_heartbeat(
                status="running",
                active_task_id=claimed.id,
                metadata={"phase": "task_claimed", "kind": claimed.kind},
            )
            renewal_state: dict[str, Any] = {"count": 0}
            renewal_task = asyncio.create_task(self._renew_lease_until_cancelled(claimed.id, renewal_state))
            try:
                output = handler(claimed)
                if inspect.isawaitable(output):
                    output = await output
                result_metadata = output or {}
            except Exception as error:
                await self._stop_renewal_task(renewal_task)
                await self._record_heartbeat(
                    status="failed",
                    active_task_id=claimed.id,
                    metadata={"phase": "handler_failed", "kind": claimed.kind, "error": str(error)},
                )
                metadata = self._worker_metadata(status="failed", result={}, renewal_state=renewal_state)
                finished = await self.harness.finish_task(
                    claimed.id,
                    status="failed",
                    error=str(error),
                    metadata=metadata,
                )
                results.append(self._result(finished or claimed, "failed", error=str(error), metadata=metadata))
                await self._record_heartbeat(
                    status="idle",
                    metadata={"phase": "task_finished", "last_task_id": claimed.id, "last_task_status": "failed"},
                )
                continue
            await self._stop_renewal_task(renewal_task)
            metadata = self._worker_metadata(
                status="succeeded",
                result=result_metadata,
                renewal_state=renewal_state,
            )
            finished = await self.harness.finish_task(
                claimed.id,
                status="succeeded",
                metadata=metadata,
            )
            results.append(self._result(finished or claimed, "succeeded", metadata=metadata))
            await self._record_heartbeat(
                status="idle",
                metadata={"phase": "task_finished", "last_task_id": claimed.id, "last_task_status": "succeeded"},
            )
        return results

    async def _renew_lease_until_cancelled(self, task_id: str, state: dict[str, Any]) -> None:
        try:
            while True:
                await asyncio.sleep(self.renewal_interval_seconds)
                record = await self.harness.renew_task_lease(
                    task_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
                state["count"] = int(state.get("count", 0)) + 1
                state["lease_version"] = record.lease_version
                await self._record_heartbeat(
                    status="running",
                    active_task_id=task_id,
                    metadata={"phase": "lease_renewed", "lease_version": record.lease_version},
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            state["error"] = str(error)

    async def _stop_renewal_task(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    def _worker_metadata(
        self,
        *,
        status: WorkerHeartbeatStatus,
        result: dict[str, Any],
        renewal_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        worker_runner = {
            "worker_id": self.worker_id,
            "status": status,
            "result": result,
            "renewal_count": int((renewal_state or {}).get("count", 0)),
        }
        if renewal_state and renewal_state.get("error"):
            worker_runner["renewal_error"] = renewal_state["error"]
        return {
            "worker_runner": {
                **worker_runner,
            }
        }

    async def _record_heartbeat(
        self,
        *,
        status: str,
        active_task_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.harness.record_worker_heartbeat(
            worker_id=self.worker_id,
            status=status,
            active_task_id=active_task_id,
            stale_after_seconds=max(self.lease_seconds * 2, 1.0),
            metadata=metadata or {},
        )

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


def scheduler_manual_trigger_handler(scheduler: Any) -> PlatformTaskHandler:
    async def handler(task: PlatformTaskRecord) -> dict[str, Any]:
        inputs = task.metadata.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        created = await scheduler.trigger_now(
            task.owner_id,
            inputs=inputs,
            harness_task_id=task.id,
            manage_harness_task=False,
        )
        return {
            "application_id": task.owner_id,
            "run_id": created["run_id"],
            "status": created.get("status", "queued"),
        }

    return handler


def scheduler_trigger_handler(scheduler: Any) -> PlatformTaskHandler:
    async def handler(task: PlatformTaskRecord) -> dict[str, Any]:
        version = _required_int_metadata(task, "version")
        node_id = _required_str_metadata(task, "node_id")
        local_date = _required_str_metadata(task, "local_date")
        triggered_at = _metadata_datetime(task.metadata.get("triggered_at"))
        event = await scheduler.execute_claimed_schedule_fire(
            task.owner_id,
            version=version,
            node_id=node_id,
            local_date=local_date,
            triggered_at=triggered_at,
            harness_task_id=task.id,
            manage_harness_task=False,
        )
        return {
            "application_id": task.owner_id,
            "run_id": event["run_id"],
            "version": version,
            "node_id": node_id,
            "local_date": local_date,
        }

    return handler


def _required_str_metadata(task: PlatformTaskRecord, key: str) -> str:
    value = task.metadata.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"scheduler_trigger task metadata requires {key}")
    return value


def _required_int_metadata(task: PlatformTaskRecord, key: str) -> int:
    value = task.metadata.get(key)
    if isinstance(value, bool):
        raise ValueError(f"scheduler_trigger task metadata requires integer {key}")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    raise ValueError(f"scheduler_trigger task metadata requires integer {key}")


def _metadata_datetime(value: Any) -> Any:
    from datetime import datetime, timezone

    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def unavailable_worker_handler(kind: str) -> PlatformTaskHandler:
    spec = UNAVAILABLE_WORKER_HANDLERS[kind]

    async def handler(_task: PlatformTaskRecord) -> dict[str, Any]:
        raise PlatformWorkerHandlerUnavailable(
            f"worker handler unavailable: {kind}; {spec['reason']}; action: {spec['operator_action']}"
        )

    return handler


def build_platform_worker_handlers(services: Any) -> dict[str, PlatformTaskHandler]:
    handlers: dict[str, PlatformTaskHandler] = {
        "scheduler_trigger": scheduler_trigger_handler(services.scheduler),
        "scheduler_manual_trigger": scheduler_manual_trigger_handler(services.scheduler),
    }
    for kind in UNAVAILABLE_WORKER_HANDLERS:
        handlers[kind] = unavailable_worker_handler(kind)
    assert_complete_platform_worker_handler_catalog(handlers)
    return handlers


def platform_worker_handler_catalog(
    handlers: dict[str, PlatformTaskHandler] | None = None,
) -> dict[str, Any]:
    registered = set(handlers or {})
    entries: list[WorkerHandlerCatalogEntry] = []
    for kind in PLATFORM_WORKER_TASK_KINDS:
        if kind in IMPLEMENTED_WORKER_HANDLERS:
            spec = IMPLEMENTED_WORKER_HANDLERS[kind]
            entries.append(
                WorkerHandlerCatalogEntry(
                    kind=kind,
                    label=spec["label"],
                    required=True,
                    status="implemented",
                    handler_registered=kind in registered,
                    executable=True,
                    implementation=spec["implementation"],
                    evidence=spec["evidence"],
                    reason="",
                    operator_action="Monitor worker-runner results and lease renewals.",
                )
            )
            continue
        spec = UNAVAILABLE_WORKER_HANDLERS[kind]
        entries.append(
            WorkerHandlerCatalogEntry(
                kind=kind,
                label=spec["label"],
                required=True,
                status="unavailable",
                handler_registered=kind in registered,
                executable=False,
                implementation="unavailable_worker_handler",
                evidence="docs/stage-reports/v0.2.110_e08_complete_handler_catalog.md",
                reason=spec["reason"],
                operator_action=spec["operator_action"],
            )
        )
    entry_dicts = [asdict(entry) for entry in entries]
    required = {entry.kind for entry in entries if entry.required}
    cataloged = {entry.kind for entry in entries}
    missing = sorted(required - cataloged)
    unregistered = sorted(kind for kind in required if kind not in registered)
    implemented = [entry.kind for entry in entries if entry.status == "implemented"]
    unavailable = [entry.kind for entry in entries if entry.status == "unavailable"]
    return {
        "version": "v0.2.114",
        "source": "docs/stage-reports/v0.2.113_e08_remaining_sidecar_slice_reselection.md",
        "required_count": len(required),
        "cataloged_count": len(cataloged),
        "implemented_count": len(implemented),
        "unavailable_count": len(unavailable),
        "missing_required_kinds": missing,
        "unregistered_required_kinds": unregistered,
        "catalog_complete": not missing,
        "registered_catalog_complete": not missing and not unregistered,
        "full_execution_coverage": len(unavailable) == 0 and not missing and not unregistered,
        "deterministic_gap_failure": not missing and not unregistered,
        "not_full_sidecar_completion": True,
        "entries": entry_dicts,
    }


def assert_complete_platform_worker_handler_catalog(handlers: dict[str, PlatformTaskHandler]) -> None:
    catalog = platform_worker_handler_catalog(handlers)
    if not catalog["catalog_complete"]:
        missing = ", ".join(catalog["missing_required_kinds"])
        raise ValueError(f"platform worker handler catalog is missing task kind(s): {missing}")
    if not catalog["registered_catalog_complete"]:
        missing = ", ".join(catalog["unregistered_required_kinds"])
        raise ValueError(f"platform worker handler registry is missing task kind(s): {missing}")


async def create_platform_worker_runner(
    *,
    worker_id: str | None = None,
    lease_seconds: float | None = None,
    renewal_interval_seconds: float | None = None,
) -> tuple[Any, PlatformHarnessWorkerRunner]:
    from .api import build_services
    from .config import get_settings

    settings = get_settings()
    settings.prepare()
    services = build_services(settings)
    await services.storage.initialize()
    await services.workflow_store.initialize()
    await services.workflow_store.fail_interrupted_runs()
    runner = PlatformHarnessWorkerRunner(
        harness=services.harness,
        worker_id=worker_id or services.harness.worker_id,
        lease_seconds=lease_seconds or max(services.harness.worker_lease_seconds, 60.0),
        renewal_interval_seconds=renewal_interval_seconds,
        handlers=build_platform_worker_handlers(services),
    )
    return services, runner


async def run_worker_once(
    *,
    worker_id: str | None = None,
    lease_seconds: float | None = None,
    renewal_interval_seconds: float | None = None,
    kind: str | None = None,
    owner_id: str | None = None,
    limit: int = 10,
) -> list[WorkerRunResult]:
    services, runner = await create_platform_worker_runner(
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        renewal_interval_seconds=renewal_interval_seconds,
    )
    try:
        return await runner.run_once(kind=kind, owner_id=owner_id, limit=limit)
    finally:
        await services.sandboxes.close()


async def _run_worker_from_args(args: argparse.Namespace) -> None:
    services, runner = await create_platform_worker_runner(
        worker_id=args.worker_id,
        lease_seconds=args.lease_seconds,
        renewal_interval_seconds=args.renewal_interval_seconds,
    )
    try:
        while True:
            results = await runner.run_once(kind=args.kind, owner_id=args.owner_id, limit=args.limit)
            print(json.dumps([asdict(result) for result in results], ensure_ascii=False))
            if args.once:
                return
            await asyncio.sleep(args.poll_seconds)
    finally:
        await services.sandboxes.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Platform Harness worker.")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--lease-seconds", type=float, default=None)
    parser.add_argument("--renewal-interval-seconds", type=float, default=None)
    parser.add_argument("--kind", default=None)
    parser.add_argument("--owner-id", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--once", action="store_true", help="Run one polling iteration and exit.")
    args = parser.parse_args()
    if not args.once:
        args.once = False
    asyncio.run(_run_worker_from_args(args))
