from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .blocks import BlockRegistry, ScheduleTriggerConfig
from .platform_harness import PlatformHarness
from .storage import Storage
from .workflow_models import WorkflowRunRequest
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import WorkflowStorage


class WorkflowScheduler:
    """Small persistent daily scheduler for published schedule-trigger workflows."""

    def __init__(
        self,
        *,
        storage: Storage,
        workflow_store: WorkflowStorage,
        blocks: BlockRegistry,
        runtime: WorkflowRuntime,
        harness: PlatformHarness,
        poll_seconds: float = 30,
    ) -> None:
        self.storage = storage
        self.workflow_store = workflow_store
        self.blocks = blocks
        self.runtime = runtime
        self.harness = harness
        self.poll_seconds = poll_seconds
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if not self.task:
            return
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        self.task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self.storage.append_event(
                    "scheduler", "scheduler.failed", {"error": str(error), "error_type": type(error).__name__}
                )
            await asyncio.sleep(self.poll_seconds)

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        started: list[dict[str, Any]] = []
        for application in await self.workflow_store.list_applications():
            if application["active_version"] is None:
                continue
            version = int(application["active_version"])
            published = await self.workflow_store.get_version(application["id"], version)
            for node in published["snapshot"].workflow.nodes:
                if node.type != "schedule_trigger":
                    continue
                config = ScheduleTriggerConfig.model_validate(node.config)
                local = now.astimezone(ZoneInfo(config.timezone))
                if (local.hour, local.minute) < (config.hour, config.minute):
                    continue
                local_date = local.date().isoformat()
                claimed = await self.workflow_store.claim_schedule_fire(
                    application["id"], version, node.id, local_date
                )
                if not claimed:
                    continue
                task_id = f"scheduler:{application['id']}:{version}:{node.id}:{local_date}"
                try:
                    await self.harness.start_task(
                        task_id,
                        kind="scheduler_trigger",
                        owner_id=application["id"],
                        resource_id=task_id,
                        metadata={
                            "version": version,
                            "node_id": node.id,
                            "local_date": local_date,
                            "timezone": config.timezone,
                        },
                    )
                    await self.harness.record_usage(
                        task_id,
                        "scheduler_fire",
                        metadata={"node_id": node.id, "local_date": local_date},
                    )
                    created = await self.runtime.create_run(
                        application["id"],
                        WorkflowRunRequest(
                            version=version,
                            inputs={
                                **config.inputs,
                                "__schedule__": {
                                    "triggered_at": now.isoformat(),
                                    "local_date": local_date,
                                    "timezone": config.timezone,
                                    "manual": False,
                                },
                            },
                        ),
                        parent_task_id=task_id,
                        origin="scheduler",
                    )
                    await self.workflow_store.complete_schedule_fire(
                        application["id"], version, node.id, local_date, created["run_id"]
                    )
                    event = {
                        "application_id": application["id"],
                        "version": version,
                        "node_id": node.id,
                        "local_date": local_date,
                        "run_id": created["run_id"],
                    }
                    await self.storage.append_event("scheduler", "scheduler.triggered", event)
                    await self.storage.append_event(
                        application["id"], "scheduler.triggered", event
                    )
                    await self.harness.finish_task(task_id, status="succeeded")
                    started.append(event)
                except Exception:
                    await self.harness.finish_task(task_id, status="failed")
                    await self.workflow_store.release_schedule_fire(
                        application["id"], version, node.id, local_date
                    )
                    raise
        return started

    async def trigger_now(
        self, application_id: str, inputs: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        published = await self.workflow_store.get_version(application_id)
        node = next(
            (item for item in published["snapshot"].workflow.nodes if item.type == "schedule_trigger"),
            None,
        )
        if not node:
            raise ValueError("published application has no schedule_trigger node")
        config = ScheduleTriggerConfig.model_validate(node.config)
        now = datetime.now(timezone.utc)
        task_id = f"scheduler-manual:{application_id}:{int(published['version'])}:{node.id}:{now.timestamp()}"
        await self.harness.start_task(
            task_id,
            kind="scheduler_manual_trigger",
            owner_id=application_id,
            resource_id=task_id,
            metadata={"version": published["version"], "node_id": node.id},
        )
        await self.harness.record_usage(
            task_id,
            "scheduler_fire",
            metadata={"node_id": node.id, "manual": True},
        )
        created = await self.runtime.create_run(
            application_id,
            WorkflowRunRequest(
                version=int(published["version"]),
                inputs={
                    **config.inputs,
                    **(inputs or {}),
                    "__schedule__": {
                        "triggered_at": now.isoformat(),
                        "timezone": config.timezone,
                        "manual": True,
                    },
                },
            ),
            parent_task_id=task_id,
            origin="scheduler_manual",
        )
        await self.storage.append_event(application_id, "scheduler.manual_triggered", {
            "application_id": application_id,
            "version": published["version"],
            "node_id": node.id,
            "run_id": created["run_id"],
        })
        await self.harness.finish_task(task_id, status="succeeded")
        return created

    async def list_schedules(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        fires = await self.workflow_store.list_schedule_fires()
        by_application: dict[str, list[dict[str, Any]]] = {}
        for fire in fires:
            by_application.setdefault(fire["application_id"], []).append(fire)
        for application in await self.workflow_store.list_applications():
            if application["active_version"] is None:
                continue
            published = await self.workflow_store.get_version(
                application["id"], int(application["active_version"])
            )
            for node in published["snapshot"].workflow.nodes:
                if node.type == "schedule_trigger":
                    config = ScheduleTriggerConfig.model_validate(node.config)
                    result.append({
                        "application_id": application["id"],
                        "application_name": application["name"],
                        "version": application["active_version"],
                        "node_id": node.id,
                        "timezone": config.timezone,
                        "hour": config.hour,
                        "minute": config.minute,
                        "last_fire": (by_application.get(application["id"]) or [None])[0],
                    })
        return result
