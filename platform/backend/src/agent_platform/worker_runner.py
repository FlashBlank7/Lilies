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
    "workflow_run": {
        "label": "Workflow run",
        "implementation": "workflow_run_handler",
        "evidence": "docs/stage-reports/v0.2.116_e08_workflow_run_worker_offload_handler.md",
    },
    "test_suite": {
        "label": "Test suite",
        "implementation": "test_suite_handler",
        "evidence": "docs/stage-reports/v0.2.118_e08_test_suite_worker_offload_handler.md",
    },
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
    "draft_patch_preview": {
        "label": "Draft patch preview",
        "implementation": "draft_patch_preview_handler",
        "evidence": "docs/stage-reports/v0.2.120_e08_draft_patch_preview_worker_offload_handler.md",
    },
    "benchmark": {
        "label": "Benchmark",
        "implementation": "benchmark_handler",
        "evidence": "docs/stage-reports/v0.2.122_e08_benchmark_worker_offload_handler.md",
    },
    "builder_build": {
        "label": "Builder build",
        "implementation": "builder_build_handler",
        "evidence": "docs/stage-reports/v0.2.124_e08_builder_build_worker_offload_handler.md",
    },
}

UNAVAILABLE_WORKER_HANDLERS: dict[str, dict[str, str]] = {}


PlatformTaskHandler = Callable[[PlatformTaskRecord], Awaitable[dict[str, Any] | None] | dict[str, Any] | None]


class PlatformWorkerHandlerUnavailable(RuntimeError):
    pass


class PlatformWorkerHandlerFailed(RuntimeError):
    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


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
        results: list[WorkerRunResult] = []
        for _ in range(max(1, limit)):
            claimed = await self.harness.claim_next_queued_task(
                kind=kind,
                owner_id=owner_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            )
            if claimed is None:
                break
            handler = self.handlers.get(claimed.kind)
            if handler is None:
                await self.harness.release_task_lease(
                    claimed.id,
                    worker_id=self.worker_id,
                    next_status="queued",
                )
                results.append(self._result(claimed, "skipped", error=f"no handler for kind: {claimed.kind}"))
                break
            results.append(await self._run_claimed_task(claimed, handler))
        if not results:
            await self._record_heartbeat(status="idle", metadata={"phase": "poll_empty"})
        return results

    async def _run_claimed_task(
        self,
        claimed: PlatformTaskRecord,
        handler: PlatformTaskHandler,
    ) -> WorkerRunResult:
        await self._record_heartbeat(
            status="running",
            active_task_id=claimed.id,
            metadata={"phase": "task_claimed", "kind": claimed.kind, "queue_claim_next": True},
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
            error_result = getattr(error, "result", {})
            if not isinstance(error_result, dict):
                error_result = {}
            metadata = self._worker_metadata(status="failed", result=error_result, renewal_state=renewal_state)
            finished = await self.harness.finish_task(
                claimed.id,
                status="failed",
                error=str(error),
                metadata=metadata,
            )
            await self._record_heartbeat(
                status="idle",
                metadata={"phase": "task_finished", "last_task_id": claimed.id, "last_task_status": "failed"},
            )
            return self._result(finished or claimed, "failed", error=str(error), metadata=metadata)
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
        await self._record_heartbeat(
            status="idle",
            metadata={"phase": "task_finished", "last_task_id": claimed.id, "last_task_status": "succeeded"},
        )
        return self._result(finished or claimed, "succeeded", metadata=metadata)

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


class PlatformWorkerSupervisor:
    """In-process supervision for a Platform Harness worker loop.

    This is intentionally not an external process manager or distributed queue
    backend. It gives operators a product-level start/observe/stop lifecycle for
    the existing worker runner and heartbeat registry.
    """

    def __init__(
        self,
        *,
        runner: PlatformHarnessWorkerRunner,
        poll_seconds: float = 5.0,
        limit: int = 10,
        background_tasks: set[asyncio.Task[Any]] | None = None,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be greater than 0")
        if limit <= 0:
            raise ValueError("limit must be greater than 0")
        self.runner = runner
        self.poll_seconds = poll_seconds
        self.limit = limit
        self.background_tasks = background_tasks
        self._task: asyncio.Task[None] | None = None
        self._stop_requested = asyncio.Event()
        self.run_count = 0
        self.last_results: list[WorkerRunResult] = []
        self.last_error = ""
        self.started_at = ""
        self.stopped_at = ""

    @property
    def loop_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        *,
        poll_seconds: float | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        if poll_seconds is not None:
            if poll_seconds <= 0:
                raise ValueError("poll_seconds must be greater than 0")
            self.poll_seconds = poll_seconds
        if limit is not None:
            if limit <= 0:
                raise ValueError("limit must be greater than 0")
            self.limit = limit
        if self.loop_running:
            return await self.snapshot()
        self._stop_requested = asyncio.Event()
        self.last_error = ""
        self.started_at = _utc_now()
        await self.runner.harness.record_worker_heartbeat(
            worker_id=self.runner.worker_id,
            status="idle",
            stale_after_seconds=max(self.runner.lease_seconds * 2, 1.0),
            metadata={
                "phase": "supervisor_starting",
                "supervision_mode": "in_process_worker_loop",
                "version": "v0.2.126",
            },
        )
        self._task = asyncio.create_task(
            self._run_loop(),
            name=f"platform-worker-supervisor:{self.runner.worker_id}",
        )
        if self.background_tasks is not None:
            self.background_tasks.add(self._task)
            self._task.add_done_callback(self.background_tasks.discard)
        return await self.snapshot()

    async def stop(self) -> dict[str, Any]:
        if not self.loop_running:
            self.stopped_at = _utc_now()
            await self.runner.harness.record_worker_heartbeat(
                worker_id=self.runner.worker_id,
                status="idle",
                stale_after_seconds=max(self.runner.lease_seconds * 2, 1.0),
                metadata={
                    "phase": "supervisor_stopped",
                    "supervision_mode": "in_process_worker_loop",
                    "idempotent": True,
                    "version": "v0.2.126",
                },
            )
            return await self.snapshot()
        await self.runner.harness.record_worker_heartbeat(
            worker_id=self.runner.worker_id,
            status="stopping",
            stale_after_seconds=max(self.runner.lease_seconds * 2, 1.0),
            metadata={
                "phase": "supervisor_stop_requested",
                "supervision_mode": "in_process_worker_loop",
                "version": "v0.2.126",
            },
        )
        self._stop_requested.set()
        assert self._task is not None
        try:
            await asyncio.wait_for(self._task, timeout=max(self.poll_seconds + 1.0, 1.0))
        except TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        finally:
            self._task = None
        self.stopped_at = _utc_now()
        await self.runner.harness.record_worker_heartbeat(
            worker_id=self.runner.worker_id,
            status="idle",
            stale_after_seconds=max(self.runner.lease_seconds * 2, 1.0),
            metadata={
                "phase": "supervisor_stopped",
                "supervision_mode": "in_process_worker_loop",
                "version": "v0.2.126",
            },
        )
        return await self.snapshot()

    async def snapshot(self) -> dict[str, Any]:
        heartbeat = await self._heartbeat()
        heartbeat_payload = heartbeat.model_dump(mode="json") if heartbeat else None
        return {
            "version": "v0.2.126",
            "source": "docs/stage-reports/v0.2.125_e08_remaining_sidecar_architecture_reselection.md",
            "worker_id": self.runner.worker_id,
            "desired_state": "running" if self.loop_running else "stopped",
            "loop_running": self.loop_running,
            "supervision_mode": "in_process_worker_loop",
            "poll_seconds": self.poll_seconds,
            "limit": self.limit,
            "run_count": self.run_count,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "observed_state": heartbeat.status if heartbeat else "unknown",
            "observed_liveness": heartbeat.liveness if heartbeat else "unknown",
            "heartbeat": heartbeat_payload,
            "recent_results": [asdict(result) for result in self.last_results[-10:]],
            "supports_start": True,
            "supports_stop": True,
            "boundaries": {
                "external_process_manager": False,
                "distributed_queue_semantics": False,
                "external_kms_provider_integration": False,
                "full_sidecar_completion_claimed": False,
            },
        }

    async def _run_loop(self) -> None:
        await self.runner.harness.record_worker_heartbeat(
            worker_id=self.runner.worker_id,
            status="idle",
            stale_after_seconds=max(self.runner.lease_seconds * 2, 1.0),
            metadata={
                "phase": "supervisor_loop_started",
                "supervision_mode": "in_process_worker_loop",
                "version": "v0.2.126",
            },
        )
        try:
            while not self._stop_requested.is_set():
                results = await self.runner.run_once(limit=self.limit)
                self.run_count += 1
                if results:
                    self.last_results.extend(results)
                    self.last_results = self.last_results[-10:]
                try:
                    await asyncio.wait_for(self._stop_requested.wait(), timeout=self.poll_seconds)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = str(error)
            await self.runner.harness.record_worker_heartbeat(
                worker_id=self.runner.worker_id,
                status="failed",
                stale_after_seconds=max(self.runner.lease_seconds * 2, 1.0),
                metadata={
                    "phase": "supervisor_loop_failed",
                    "supervision_mode": "in_process_worker_loop",
                    "error": str(error),
                    "version": "v0.2.126",
                },
            )
            raise
        finally:
            await self.runner.harness.record_worker_heartbeat(
                worker_id=self.runner.worker_id,
                status="idle",
                stale_after_seconds=max(self.runner.lease_seconds * 2, 1.0),
                metadata={
                    "phase": "supervisor_loop_exit",
                    "supervision_mode": "in_process_worker_loop",
                    "version": "v0.2.126",
                },
            )

    async def _heartbeat(self) -> Any:
        rows = await self.runner.harness.list_worker_heartbeats()
        for row in rows:
            if row.worker_id == self.runner.worker_id:
                return row
        return None


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


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


def workflow_run_handler(workflow_runtime: Any) -> PlatformTaskHandler:
    async def handler(task: PlatformTaskRecord) -> dict[str, Any]:
        from .workflow_models import WorkflowRunRequest

        inputs = task.metadata.get("inputs", {})
        if not isinstance(inputs, dict):
            inputs = {}
        workspace_path = task.metadata.get("workspace_path", ".")
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            workspace_path = "."
        version = task.metadata.get("version")
        if isinstance(version, bool):
            version = None
        elif isinstance(version, str) and version.strip().isdigit():
            version = int(version)
        elif not isinstance(version, int):
            version = None
        use_draft = bool(task.metadata.get("use_draft", version is None))
        created = await workflow_runtime.create_run(
            task.owner_id,
            WorkflowRunRequest(
                inputs=inputs,
                version=version,
                use_draft=use_draft,
                workspace_path=workspace_path,
            ),
            parent_task_id=task.id,
            origin="worker",
        )
        return {
            "application_id": task.owner_id,
            "run_id": created["run_id"],
            "status": created.get("status", "queued"),
            "version": created.get("version"),
            "draft_revision": created.get("draft_revision"),
        }

    return handler


def test_suite_handler(workflow_runtime: Any) -> PlatformTaskHandler:
    async def handler(task: PlatformTaskRecord) -> dict[str, Any]:
        report = await workflow_runtime.run_test_suite(
            task.owner_id,
            harness_task_id=task.id,
            manage_harness_task=False,
            origin="test_suite",
        )
        summary = report.get("summary", {})
        return {
            "application_id": task.owner_id,
            "passed": report.get("passed", False),
            "total": summary.get("total", 0),
            "failed": summary.get("failed", 0),
            "mandatory_failed": summary.get("mandatory_failed", 0),
            "test_run_ids": [
                item.get("run_id")
                for item in report.get("tests", [])
                if item.get("run_id")
            ],
        }

    return handler


def draft_patch_preview_handler(workflow_store: Any, draft_patcher: Any) -> PlatformTaskHandler:
    async def handler(task: PlatformTaskRecord) -> dict[str, Any]:
        instruction = task.metadata.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("draft_patch_preview task metadata requires instruction")
        draft = await workflow_store.get_draft(task.owner_id)
        response = draft_patcher.preview(
            draft["snapshot"],
            int(draft["revision"]),
            instruction,
        )
        result = response.model_dump(mode="json")
        if not response.supported:
            raise PlatformWorkerHandlerUnavailable(
                f"worker draft_patch_preview unsupported: {response.message}"
            )
        return {
            "application_id": task.owner_id,
            "revision": int(draft["revision"]),
            "content_hash": draft["content_hash"],
            **result,
        }

    return handler


def benchmark_handler(benchmark: Any, harness: PlatformHarness) -> PlatformTaskHandler:
    async def handler(task: PlatformTaskRecord) -> dict[str, Any]:
        from .builder_benchmark import (  # pylint: disable=import-outside-toplevel
            BuilderBenchmarkCase,
            BuilderBenchmarkSuiteCase,
        )

        case_payload = task.metadata.get("case_payload")
        suite_payload = task.metadata.get("suite_payload")
        if case_payload is None and isinstance(task.metadata.get("case"), dict):
            case_payload = task.metadata.get("case")
        if suite_payload is None and isinstance(task.metadata.get("suite"), dict):
            suite_payload = task.metadata.get("suite")
        if case_payload is not None and suite_payload is not None:
            raise ValueError("benchmark task metadata requires exactly one of case_payload or suite_payload")
        if suite_payload is not None:
            suite = BuilderBenchmarkSuiteCase.model_validate(suite_payload)
            await harness.record_usage(
                task.id,
                "node_execution",
                amount=max(1, len(suite.cases)),
                metadata={"operation": "builder_benchmark_suite", "case_count": len(suite.cases)},
            )
            report = benchmark.evaluate_suite(suite)
            result = {
                "mode": "suite",
                "name": suite.name,
                "passed": report.passed,
                "score": report.score,
                "pass_rate": report.pass_rate,
                "case_count": report.case_count,
                "failed_cases": report.failed_cases,
                "report": report.model_dump(mode="json"),
            }
            if not report.passed:
                raise PlatformWorkerHandlerFailed(
                    f"worker benchmark suite failed: {suite.name}",
                    result,
                )
            return result
        if case_payload is not None:
            case = BuilderBenchmarkCase.model_validate(case_payload)
            report = benchmark.evaluate(case)
            result = {
                "mode": "case",
                "name": case.name,
                "passed": report.passed,
                "score": report.score,
                "missing": report.missing,
                "report": report.model_dump(mode="json"),
            }
            if not report.passed:
                raise PlatformWorkerHandlerFailed(
                    f"worker benchmark case failed: {case.name}",
                    result,
                )
            return result
        raise ValueError("benchmark task metadata requires case_payload or suite_payload")

    return handler


def builder_build_handler(builder: Any) -> PlatformTaskHandler:
    async def handler(task: PlatformTaskRecord) -> dict[str, Any]:
        build_id = task.metadata.get("build_id") or task.resource_id or task.id
        if not isinstance(build_id, str) or not build_id.strip():
            raise ValueError("builder_build task metadata requires build_id or resource_id")
        build_id = build_id.strip()
        if build_id != task.id:
            raise ValueError("builder_build worker task id must match build_id")
        try:
            result = await builder.run_claimed_build(build_id)
        except Exception as error:
            failure_result: dict[str, Any] = {"build_id": build_id}
            try:
                build = await builder.workflow_store.get_build(build_id)
                failure_result.update({
                    "application_id": build["application_id"],
                    "status": build["status"],
                    "error": build.get("error") or str(error),
                })
            except Exception:
                failure_result["error"] = str(error)
            raise PlatformWorkerHandlerFailed(
                f"worker builder_build failed: {build_id}: {error}",
                failure_result,
            ) from error
        return result

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
        "workflow_run": workflow_run_handler(services.workflow_runtime),
        "test_suite": test_suite_handler(services.workflow_runtime),
        "scheduler_trigger": scheduler_trigger_handler(services.scheduler),
        "scheduler_manual_trigger": scheduler_manual_trigger_handler(services.scheduler),
        "draft_patch_preview": draft_patch_preview_handler(services.workflow_store, services.draft_patcher),
        "benchmark": benchmark_handler(services.benchmark, services.harness),
        "builder_build": builder_build_handler(services.builder),
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
        "version": "v0.2.124",
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
