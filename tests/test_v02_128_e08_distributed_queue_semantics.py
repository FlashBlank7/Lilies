from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage
from agent_platform.worker_runner import PlatformHarnessWorkerRunner
from tests.test_runtime import ScriptedProvider


def run(coro):
    return asyncio.run(coro)


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


async def queue_task(harness: PlatformHarness, task_id: str, *, worker_id: str = "producer") -> None:
    await harness.start_task(
        task_id,
        kind="scheduler_manual_trigger",
        owner_id="owner-a",
        resource_id=f"schedule-{task_id}",
        worker_id=worker_id,
        lease_seconds=60,
    )
    await harness.release_task_lease(task_id, worker_id=worker_id, next_status="queued")


def test_v02_128_claim_next_is_single_owner_and_fifo(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await queue_task(harness, "queue-task-1")
        await queue_task(harness, "queue-task-2")

        first, second, third = await asyncio.gather(
            harness.claim_next_queued_task(worker_id="worker-a", lease_seconds=60),
            harness.claim_next_queued_task(worker_id="worker-b", lease_seconds=60),
            harness.claim_next_queued_task(worker_id="worker-c", lease_seconds=60),
        )

        assert first is not None
        assert second is not None
        assert third is None
        assert {first.id, second.id} == {"queue-task-1", "queue-task-2"}
        assert first.worker_id != second.worker_id
        assert first.metadata["worker_lease"]["queue_claimed"] is True
        assert second.metadata["worker_lease"]["queue_claimed"] is True

    run(scenario())


def test_v02_128_expired_lease_requeues_without_failure(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=0.001)
        await queue_task(harness, "requeue-task-1")
        claimed = await harness.claim_next_queued_task(worker_id="worker-a", lease_seconds=0.001)
        assert claimed is not None
        await asyncio.sleep(0.01)

        requeued = await harness.requeue_expired_task_leases()
        assert [task.id for task in requeued] == ["requeue-task-1"]
        task = await harness.get_task("requeue-task-1")
        assert task.status == "queued"
        assert task.worker_id is None
        assert task.error == ""
        assert task.metadata["worker_lease"]["requeued"] is True
        assert task.metadata["worker_lease"]["requeued_from_worker_id"] == "worker-a"

    run(scenario())


def test_v02_128_worker_runner_consumes_queue_claim_next(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await queue_task(harness, "runner-queue-task-1")

        async def handler(record):
            return {"handled_task_id": record.id}

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="runner-worker",
            lease_seconds=60,
            handlers={"scheduler_manual_trigger": handler},
        )
        results = await runner.run_once(limit=5)

        assert [(item.task_id, item.status) for item in results] == [("runner-queue-task-1", "succeeded")]
        task = await harness.get_task("runner-queue-task-1")
        assert task.status == "succeeded"
        assert task.metadata["worker_lease"]["queue_claimed"] is True
        heartbeats = {row.worker_id: row for row in await harness.list_worker_heartbeats()}
        assert heartbeats["runner-worker"].metadata["last_task_status"] == "succeeded"

    run(scenario())


def test_v02_128_queue_semantics_api_snapshot_and_requeue(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_worker_id="api-worker",
        platform_harness_worker_lease_seconds=0.001,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        async def prepare() -> None:
            harness = client.app.state.services.harness
            await queue_task(harness, "api-queue-task-1", worker_id="producer")
            claimed = await harness.claim_next_queued_task(worker_id="api-worker", lease_seconds=0.001)
            assert claimed is not None
            await asyncio.sleep(0.01)

        client.portal.call(prepare)
        requeued = client.post("/api/v1/platform/harness/queue/requeue-expired", headers=headers())
        assert requeued.status_code == 200, requeued.text
        assert requeued.json()[0]["id"] == "api-queue-task-1"
        assert requeued.json()[0]["status"] == "queued"

        snapshot = client.get("/api/v1/platform/harness/queue-semantics", headers=headers())
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert body["queue_mode"] == "storage_backed_claim_next_with_requeue"
        assert body["claim_next_atomic"] is True
        assert body["expired_lease_requeue"] is True
        assert body["task_counts"]["queued"] == 1
        assert body["boundaries"]["external_process_manager"] is False
        assert body["boundaries"]["external_kms_provider_integration"] is False
        assert body["boundaries"]["full_sidecar_completion_claimed"] is False
