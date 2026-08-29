from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
from uuid import uuid4

from .blocks import BlockRegistry, ScheduleTriggerConfig
from .durable_jobs import (
    DurableJobConflict,
    DurableJobRecord,
    DurableJobStore,
    DurableTriggerKind,
    durable_job_id,
)
from .platform_harness import PlatformHarness
from .storage import Storage
from .workflow_models import WorkflowRunRequest
from .workflow_runtime import WorkflowRuntime
from .workflow_storage import WorkflowStorage


logger = logging.getLogger(__name__)


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
        durable_jobs: DurableJobStore,
        poll_seconds: float = 30,
        worker_offload_enabled: bool = False,
    ) -> None:
        self.storage = storage
        self.workflow_store = workflow_store
        self.blocks = blocks
        self.runtime = runtime
        self.harness = harness
        self.durable_jobs = durable_jobs
        self.poll_seconds = poll_seconds
        self.worker_offload_enabled = worker_offload_enabled
        self.durable_worker_id = f"scheduler:{harness.worker_id}"
        self.task: asyncio.Task[None] | None = None
        # 心跳：外面要能判断"调度器还在不在"。体检报「定时没按时开火」时，
        # 用户的下一步是查调度器——没有这个信号那条建议就没法落地。
        self.last_tick_at: datetime | None = None
        self.last_error: str = ""
        self.tick_count: int = 0
        self.restart_count: int = 0

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._supervise())

    async def _supervise(self) -> None:
        """循环挂了要自己爬起来。

        实测缺陷：_loop 的 except 里裸调 append_event——它自己撞锁抛异常时，
        异常从 except 块里逃出去，整个调度线程永久死亡，定时任务全体静默停摆。
        """
        while True:
            try:
                await self._loop()
            except asyncio.CancelledError:
                raise
            except BaseException as error:  # noqa: BLE001 - 循环绝不能就这么没了
                self.restart_count += 1
                self.last_error = f"循环异常重启（第 {self.restart_count} 次）：{error}"[:300]
                logger.exception("调度循环异常，%s 秒后重启", min(self.poll_seconds, 5))
                await asyncio.sleep(min(self.poll_seconds, 5))

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
                # 不在这里清 last_error：tick 会把「这一轮跳过了谁」写进去，
                # 清掉就等于把它刚说的话抹了。tick 自己负责干净时置空。
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"[:300]
                try:
                    await self.storage.append_event(
                        "scheduler", "scheduler.failed",
                        {"error": str(error), "error_type": type(error).__name__})
                except Exception:  # noqa: BLE001 - 记事失败不能反杀循环
                    logger.exception("调度器记事失败（原始错误：%s）", self.last_error)
            # 出错也算跑过一轮：循环还活着这件事本身就是要报的信号
            self.last_tick_at = datetime.now(timezone.utc)
            self.tick_count += 1
            await asyncio.sleep(self.poll_seconds)

    def health(self) -> dict[str, Any]:
        """调度器自身的死活。alive 的判据是"最近一轮在两个轮询周期内"。"""
        running = bool(self.task and not self.task.done())
        last = self.last_tick_at
        stale_after = max(self.poll_seconds * 2, 60)
        behind = None if last is None else (
            datetime.now(timezone.utc) - last).total_seconds()
        alive = running and behind is not None and behind <= stale_after
        return {
            "running": running,
            "alive": alive,
            "last_tick_at": last.isoformat() if last else None,
            "seconds_since_tick": None if behind is None else round(behind, 1),
            "poll_seconds": self.poll_seconds,
            "tick_count": self.tick_count,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
        }

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now(timezone.utc)
        await self.reconcile_durable_jobs(now)
        started: list[dict[str, Any]] = []
        # 一个工作流出问题，不能连累别的工作流开火。
        #
        # 原来整个循环体是**裸的**：get_version 查不到版本、时区名不认识
        # （配置校验会抛）、建运行时炸了（execute_claimed_schedule_fire
        # 释放认领之后照样再抛出去）——任何一个都直接掀翻整个 tick，
        # 排在它后面的工作流这一轮全部不开火。
        # 而 list_applications 的顺序是稳定的，于是下一轮它还是先炸：
        # 后面那些**永远**不开火。
        #
        # 更要命的是这事不响：_loop 捕获后照样更新 last_tick_at、
        # 照样 tick_count += 1，health() 于是报「调度器活着」。
        # 体检那条路早就防住了时区（「时区名不认识就按 UTC 算，别整个不判」），
        # 调度器这条路没防——闸只装在一个出口上。
        troubles: list[str] = []
        for application in await self.workflow_store.list_applications():
            if application["active_version"] is None:
                continue
            try:
                started.extend(await self._tick_application(application, now))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 一个坏的不能停掉全部
                who = application.get("name") or application.get("id") or "?"
                troubles.append(f"「{who}」：{type(error).__name__}: {error}")
                logger.exception("定时跳过「%s」（别的工作流照常）", who)
        started.extend(await self.run_due_durable_jobs(now=now))
        # 跳过要留痕：静默跳过等于这个定时任务无声脱离监控。
        # last_error 由 tick 自己写（_loop 不再无条件清空），
        # 否则这句话会被下一行代码抹掉。
        self.last_error = ("这一轮跳过了 " + "；".join(troubles)[:280]) if troubles else ""
        return started

    async def _tick_application(
        self, application: dict[str, Any], now: datetime
    ) -> list[dict[str, Any]]:
        started: list[dict[str, Any]] = []
        version = int(application["active_version"])
        published = await self.workflow_store.get_version(application["id"], version)
        for node in published["snapshot"].workflow.nodes:
            if node.type == "schedule_trigger":
                config = ScheduleTriggerConfig.model_validate(node.config)
                local = now.astimezone(ZoneInfo(config.timezone))
                if (local.hour, local.minute) < (config.hour, config.minute):
                    continue
                local_date = local.date().isoformat()
                if config.durable:
                    job = await self.enqueue_durable_schedule(
                        application["id"],
                        version=version,
                        node_id=node.id,
                        local_date=local_date,
                        triggered_at=now,
                        config=config,
                    )
                    started.append(
                        {
                            "application_id": application["id"],
                            "version": version,
                            "node_id": node.id,
                            "local_date": local_date,
                            "job_id": job.id,
                            "durable": True,
                            "status": job.status,
                        }
                    )
                    continue
                claimed = await self.workflow_store.claim_schedule_fire(
                    application["id"], version, node.id, local_date
                )
                if not claimed:
                    continue
                task_id = f"scheduler:{application['id']}:{version}:{node.id}:{local_date}"
                if self.worker_offload_enabled:
                    started.append(
                        await self.queue_claimed_schedule_fire(
                            application["id"],
                            version=version,
                            node_id=node.id,
                            local_date=local_date,
                            triggered_at=now,
                            timezone=config.timezone,
                            task_id=task_id,
                        )
                    )
                    continue
                started.append(
                    await self.execute_claimed_schedule_fire(
                        application["id"],
                        version=version,
                        node_id=node.id,
                        local_date=local_date,
                        triggered_at=now,
                        harness_task_id=task_id,
                        manage_harness_task=True,
                    )
                )
        return started

    async def enqueue_durable_schedule(
        self,
        application_id: str,
        *,
        version: int,
        node_id: str,
        local_date: str,
        triggered_at: datetime,
        config: ScheduleTriggerConfig,
        trigger_kind: DurableTriggerKind = "schedule",
        idempotency_key: str | None = None,
        input_overrides: dict[str, Any] | None = None,
    ) -> DurableJobRecord:
        # 幂等键里**不带版本号**：带的话，业主当天重新发布一次就换一个键，
        # 当天再开一炮（发布几次开几炮）。非 durable 那条路已经因为
        # 同一个原因修过（workflow_storage.claim_schedule_fire），两条路要一致。
        identity = idempotency_key or (
            f"schedule:{application_id}:{node_id}:{local_date}"
        )
        payload = {
            "inputs": {**config.inputs, **(input_overrides or {})},
            "timezone": config.timezone,
            "triggered_at": triggered_at.astimezone(timezone.utc).isoformat(),
            "local_date": local_date,
            "lease_seconds": config.lease_seconds,
            "manual": trigger_kind == "manual",
        }
        job = await self.durable_jobs.enqueue(
            job_id=durable_job_id(identity),
            idempotency_key=identity,
            application_id=application_id,
            version=version,
            node_id=node_id,
            trigger_kind=trigger_kind,
            local_date=local_date,
            payload=payload,
            max_attempts=config.max_attempts,
            retry_backoff_seconds=config.retry_backoff_seconds,
            available_at=triggered_at,
        )
        await self.storage.append_event(
            application_id,
            "scheduler.durable_enqueued",
            {
                "job_id": job.id,
                "idempotency_key": identity,
                "node_id": node_id,
                "local_date": local_date,
                "status": job.status,
            },
        )
        return job

    async def run_due_durable_jobs(
        self,
        *,
        now: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        current = now or datetime.now(timezone.utc)
        launched: list[dict[str, Any]] = []
        for _ in range(max(1, min(limit, 200))):
            job = await self.durable_jobs.claim_next(
                worker_id=self.durable_worker_id,
                lease_seconds=60,
                now=current,
            )
            if job is None:
                break
            launched.append(await self._launch_durable_job(job))
        return launched

    async def _launch_durable_job(self, job: DurableJobRecord) -> dict[str, Any]:
        task_id = job.platform_task_id or f"{job.id}:attempt:{job.attempt_count}"
        lease_seconds = max(float(job.payload.get("lease_seconds", 60)), 1.0)
        try:
            await self.harness.start_task(
                task_id,
                kind="scheduler_trigger",
                owner_id=job.application_id,
                resource_id=job.id,
                metadata={
                    "durable_job_id": job.id,
                    "attempt": job.attempt_count,
                    "version": job.version,
                    "node_id": job.node_id,
                    "local_date": job.local_date,
                    "trigger_kind": job.trigger_kind,
                    "lease_version": job.lease_version,
                },
                worker_id=self.durable_worker_id,
                lease_seconds=lease_seconds,
            )
            await self.harness.record_usage(
                task_id,
                "scheduler_fire",
                metadata={
                    "job_id": job.id,
                    "node_id": job.node_id,
                    "attempt": job.attempt_count,
                },
            )
            created = await self.runtime.create_run(
                job.application_id,
                WorkflowRunRequest(
                    version=job.version,
                    inputs={
                        **dict(job.payload.get("inputs", {})),
                        "__schedule__": {
                            "triggered_at": job.payload.get("triggered_at"),
                            "local_date": job.local_date,
                            "timezone": job.payload.get("timezone"),
                            "manual": bool(job.payload.get("manual")),
                            "durable": True,
                        },
                        "__job__": {
                            "job_id": job.id,
                            "worker_id": self.durable_worker_id,
                            "lease_version": job.lease_version,
                            "attempt": job.attempt_count,
                            "idempotency_key": job.idempotency_key,
                            "checkpoint": job.checkpoint,
                        },
                    },
                ),
                parent_task_id=task_id,
                origin="durable_scheduler",
            )
            attached = await self.durable_jobs.attach_run(
                job.id,
                worker_id=self.durable_worker_id,
                lease_version=job.lease_version,
                run_id=created["run_id"],
            )
            event = {
                "application_id": job.application_id,
                "version": job.version,
                "node_id": job.node_id,
                "local_date": job.local_date,
                "job_id": job.id,
                "run_id": created["run_id"],
                "attempt": job.attempt_count,
                "durable": True,
                "status": attached.status,
            }
            await self.storage.append_event(
                job.application_id,
                "scheduler.durable_started",
                event,
            )
            return event
        except Exception as error:
            try:
                await self.harness.finish_task(task_id, status="failed", error=str(error))
            except Exception:
                pass
            try:
                failed = await self.durable_jobs.fail(
                    job.id,
                    worker_id=self.durable_worker_id,
                    lease_version=job.lease_version,
                    error=str(error),
                    retryable=True,
                )
            except DurableJobConflict:
                failed = await self.durable_jobs.get(job.id)
            return {
                "application_id": job.application_id,
                "job_id": job.id,
                "attempt": job.attempt_count,
                "durable": True,
                "status": failed.status,
                "error": str(error),
            }

    async def reconcile_durable_jobs(
        self, now: datetime | None = None
    ) -> list[DurableJobRecord]:
        current = now or datetime.now(timezone.utc)
        changed: list[DurableJobRecord] = []
        running = await self.durable_jobs.list(statuses={"running"}, limit=200)
        for job in running:
            run: dict[str, Any] | None = None
            if job.run_id:
                try:
                    run = await self.workflow_store.get_run(job.run_id)
                except KeyError:
                    run = None
            if run and run["status"] in {"succeeded", "failed", "cancelled", "paused"}:
                reconciled = await self.durable_jobs.reconcile_run(
                    job.id,
                    run_id=job.run_id or "",
                    run_status=run["status"],
                    outputs=run.get("outputs", {}),
                    error=str(run.get("error") or ""),
                )
                await self._finish_durable_attempt_task(job, reconciled)
                changed.append(reconciled)
                continue
            if job.cancel_requested:
                if job.run_id:
                    try:
                        self.runtime.cancel(job.run_id)
                    except KeyError:
                        if run is not None:
                            reconciled = await self.durable_jobs.reconcile_run(
                                job.id,
                                run_id=job.run_id,
                                run_status="cancelled",
                                error="cancelled after active process was unavailable",
                            )
                            await self._finish_durable_attempt_task(job, reconciled)
                            changed.append(reconciled)
                continue
            lease_expired = bool(
                job.lease_expires_at
                and self._parse_time(job.lease_expires_at) <= current.astimezone(timezone.utc)
            )
            if lease_expired:
                if job.run_id:
                    try:
                        self.runtime.cancel(job.run_id)
                    except KeyError:
                        pass
                recovered = await self.durable_jobs.recover_expired(job.id, now=current)
                await self._finish_durable_attempt_task(job, recovered)
                changed.append(recovered)
                continue
            lease_seconds = max(float(job.payload.get("lease_seconds", 60)), 1.0)
            try:
                renewed = await self.durable_jobs.renew(
                    job.id,
                    worker_id=self.durable_worker_id,
                    lease_version=job.lease_version,
                    lease_seconds=lease_seconds,
                )
                if job.platform_task_id:
                    await self.harness.renew_task_lease(
                        job.platform_task_id,
                        worker_id=self.durable_worker_id,
                        lease_seconds=lease_seconds,
                    )
                changed.append(renewed)
            except (DurableJobConflict, KeyError):
                continue
        return changed

    async def _finish_durable_attempt_task(
        self,
        prior: DurableJobRecord,
        current: DurableJobRecord,
    ) -> None:
        if not prior.platform_task_id:
            return
        status = (
            "succeeded"
            if current.status == "succeeded"
            else "paused"
            if current.status == "paused"
            else "cancelled"
            if current.status == "cancelled"
            else "failed"
        )
        await self.harness.finish_task(
            prior.platform_task_id,
            status=status,
            error=current.error,
            metadata={
                "durable_job_status": current.status,
                "retry_scheduled": current.status == "retry_wait",
                "next_attempt_at": current.next_attempt_at,
                "alert": current.alert,
            },
        )

    async def cancel_durable_job(
        self, job_id: str, *, expected_revision: int
    ) -> DurableJobRecord:
        record = await self.durable_jobs.request_cancel(
            job_id,
            expected_revision=expected_revision,
        )
        if record.status == "running" and record.run_id:
            try:
                self.runtime.cancel(record.run_id)
            except KeyError:
                return await self.durable_jobs.reconcile_run(
                    record.id,
                    run_id=record.run_id,
                    run_status="cancelled",
                    error="cancelled after active process was unavailable",
                )
        return record

    async def retry_durable_job(
        self, job_id: str, *, expected_revision: int
    ) -> DurableJobRecord:
        record = await self.durable_jobs.retry(job_id, expected_revision=expected_revision)
        await self.run_due_durable_jobs()
        return await self.durable_jobs.get(record.id)

    async def resume_durable_job(
        self, job_id: str, *, expected_revision: int
    ) -> DurableJobRecord:
        record = await self.durable_jobs.resume(job_id, expected_revision=expected_revision)
        await self.run_due_durable_jobs()
        return await self.durable_jobs.get(record.id)

    async def queue_claimed_schedule_fire(
        self,
        application_id: str,
        *,
        version: int,
        node_id: str,
        local_date: str,
        triggered_at: datetime,
        timezone: str,
        task_id: str,
    ) -> dict[str, Any]:
        try:
            await self.harness.start_task(
                task_id,
                kind="scheduler_trigger",
                owner_id=application_id,
                resource_id=task_id,
                metadata={
                    "version": version,
                    "node_id": node_id,
                    "local_date": local_date,
                    "timezone": timezone,
                    "triggered_at": triggered_at.isoformat(),
                    "worker_offload": True,
                },
                worker_id="scheduler",
                lease_seconds=max(self.harness.worker_lease_seconds, 60.0),
            )
            await self.harness.release_task_lease(
                task_id,
                worker_id="scheduler",
                next_status="queued",
            )
            event = {
                "application_id": application_id,
                "version": version,
                "node_id": node_id,
                "local_date": local_date,
                "task_id": task_id,
                "queued": True,
            }
            await self.storage.append_event("scheduler", "scheduler.trigger_queued", event)
            await self.storage.append_event(application_id, "scheduler.trigger_queued", event)
            return event
        except Exception:
            await self.harness.finish_task(task_id, status="failed")
            await self.workflow_store.release_schedule_fire(application_id, version, node_id, local_date)
            raise

    async def execute_claimed_schedule_fire(
        self,
        application_id: str,
        *,
        version: int,
        node_id: str,
        local_date: str,
        triggered_at: datetime,
        harness_task_id: str,
        manage_harness_task: bool = False,
    ) -> dict[str, Any]:
        published = await self.workflow_store.get_version(application_id, version)
        node = next(
            (
                item
                for item in published["snapshot"].workflow.nodes
                if item.id == node_id and item.type == "schedule_trigger"
            ),
            None,
        )
        if not node:
            raise ValueError(f"published application has no schedule_trigger node: {node_id}")
        config = ScheduleTriggerConfig.model_validate(node.config)
        try:
            if manage_harness_task:
                await self.harness.start_task(
                    harness_task_id,
                    kind="scheduler_trigger",
                    owner_id=application_id,
                    resource_id=harness_task_id,
                    metadata={
                        "version": version,
                        "node_id": node_id,
                        "local_date": local_date,
                        "timezone": config.timezone,
                    },
                )
            await self.harness.record_usage(
                harness_task_id,
                "scheduler_fire",
                metadata={"node_id": node_id, "local_date": local_date},
            )
            created = await self.runtime.create_run(
                application_id,
                WorkflowRunRequest(
                    version=version,
                    inputs={
                        **config.inputs,
                        "__schedule__": {
                            "triggered_at": triggered_at.isoformat(),
                            "local_date": local_date,
                            "timezone": config.timezone,
                            "manual": False,
                        },
                    },
                ),
                parent_task_id=harness_task_id,
                origin="scheduler",
            )
            await self.workflow_store.complete_schedule_fire(
                application_id, version, node_id, local_date, created["run_id"]
            )
            event = {
                "application_id": application_id,
                "version": version,
                "node_id": node_id,
                "local_date": local_date,
                "run_id": created["run_id"],
            }
            await self.storage.append_event("scheduler", "scheduler.triggered", event)
            await self.storage.append_event(application_id, "scheduler.triggered", event)
            if manage_harness_task:
                await self.harness.finish_task(harness_task_id, status="succeeded")
            return event
        except Exception:
            if manage_harness_task:
                await self.harness.finish_task(harness_task_id, status="failed")
            await self.workflow_store.release_schedule_fire(application_id, version, node_id, local_date)
            raise

    async def trigger_now(
        self,
        application_id: str,
        inputs: dict[str, Any] | None = None,
        *,
        harness_task_id: str | None = None,
        manage_harness_task: bool = True,
        idempotency_key: str | None = None,
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
        if config.durable:
            local_date = now.astimezone(ZoneInfo(config.timezone)).date().isoformat()
            manual_key = idempotency_key or f"manual:{application_id}:{uuid4()}"
            job = await self.enqueue_durable_schedule(
                application_id,
                version=int(published["version"]),
                node_id=node.id,
                local_date=local_date,
                triggered_at=now,
                config=config,
                trigger_kind="manual",
                idempotency_key=manual_key,
                input_overrides=inputs,
            )
            await self.run_due_durable_jobs(now=now)
            current = await self.durable_jobs.get(job.id)
            return {
                "job_id": current.id,
                "status": current.status,
                "run_id": current.run_id,
                "version": current.version,
                "durable": True,
                "revision": current.revision,
            }
        task_id = harness_task_id or (
            f"scheduler-manual:{application_id}:{int(published['version'])}:{node.id}:{now.timestamp()}"
        )
        if manage_harness_task:
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
            "task_id": task_id,
        })
        if manage_harness_task:
            await self.harness.finish_task(task_id, status="succeeded")
        return created

    async def list_schedules(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        fires = await self.workflow_store.list_schedule_fires()
        durable_jobs = await self.durable_jobs.list(limit=200)
        by_application: dict[str, list[dict[str, Any]]] = {}
        for fire in fires:
            by_application.setdefault(fire["application_id"], []).append(fire)
        jobs_by_application: dict[str, list[DurableJobRecord]] = {}
        for job in durable_jobs:
            jobs_by_application.setdefault(job.application_id, []).append(job)
        for application in await self.workflow_store.list_applications():
            if application["active_version"] is None:
                continue
            published = await self.workflow_store.get_version(
                application["id"], int(application["active_version"])
            )
            for node in published["snapshot"].workflow.nodes:
                if node.type == "schedule_trigger":
                    config = ScheduleTriggerConfig.model_validate(node.config)
                    now = datetime.now(timezone.utc)
                    latest_job = (jobs_by_application.get(application["id"]) or [None])[0]
                    result.append({
                        "application_id": application["id"],
                        "application_name": application["name"],
                        "version": application["active_version"],
                        "node_id": node.id,
                        "timezone": config.timezone,
                        "hour": config.hour,
                        "minute": config.minute,
                        "durable": config.durable,
                        "max_attempts": config.max_attempts,
                        "retry_backoff_seconds": config.retry_backoff_seconds,
                        "lease_seconds": config.lease_seconds,
                        "next_fire_at": self._next_fire(config, now).isoformat(),
                        "last_fire": (by_application.get(application["id"]) or [None])[0],
                        "latest_job": (
                            latest_job.model_dump(mode="json") if latest_job else None
                        ),
                    })
        return result

    async def schedule_status(self, application_id: str) -> dict[str, Any]:
        draft = await self.workflow_store.get_draft(application_id)
        draft_has_schedule = any(
            node.type == "schedule_trigger"
            for node in draft["snapshot"].workflow.nodes
        )
        schedules = [
            item for item in await self.list_schedules() if item["application_id"] == application_id
        ]
        jobs = await self.durable_jobs.list(application_id, limit=50)
        latest = jobs[0] if jobs else None
        return {
            "application_id": application_id,
            "status": (
                "active"
                if schedules
                else "draft_unpublished"
                if draft_has_schedule
                else "not_configured"
            ),
            "draft_has_schedule": draft_has_schedule,
            "schedule": schedules[0] if schedules else None,
            "job_count": len(jobs),
            "active_job_count": sum(
                item.status in {"queued", "running", "retry_wait", "paused"} for item in jobs
            ),
            "latest_job": latest.model_dump(mode="json") if latest else None,
            "latest_alert": next((item.alert for item in jobs if item.alert), None),
        }

    @staticmethod
    def _next_fire(config: ScheduleTriggerConfig, now: datetime) -> datetime:
        local_now = now.astimezone(ZoneInfo(config.timezone))
        candidate = local_now.replace(
            hour=config.hour,
            minute=config.minute,
            second=0,
            microsecond=0,
        )
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
