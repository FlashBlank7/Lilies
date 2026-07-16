from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.blocks import (
    CollectionDigestConfig,
    WebCollectionConfig,
    build_block_registry,
)
from agent_platform.config import Settings
from agent_platform.durable_jobs import DurableJobConflict, DurableJobStore
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.platform_harness import PlatformHarness
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.storage import Storage
from agent_platform.web_collection import ControlledWebCollector


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {
    "Authorization": "Bearer durable-test",
    "Content-Type": "application/json",
}


class NoopProvider(ModelProvider):
    name = "durable-noop"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 1}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": ""}},
        )
        yield StreamEvent(
            type="content_block_delta",
            data={"index": 0, "delta": {"type": "text_delta", "text": "done"}},
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        )


class CollectionFixtureHandler(BaseHTTPRequestHandler):
    pages = {
        "/alpha": "Alpha version one",
        "/beta": "Beta stable",
        "/flaky": "Recovered source",
    }
    flaky_failures = 0
    robots_calls = 0

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/robots.txt":
            type(self).robots_calls += 1
            self._send(200, "User-agent: *\nDisallow: /blocked\n", "text/plain")
            return
        if path == "/blocked":
            self._send(200, "Blocked content")
            return
        if path == "/oversized":
            self._send(200, "x" * 2048)
            return
        if path == "/flaky" and type(self).flaky_failures > 0:
            type(self).flaky_failures -= 1
            self._send(503, "temporary failure", "text/plain")
            return
        if path in type(self).pages:
            body = (
                f"<html><head><title>{path[1:].title()}</title></head>"
                f"<body><main>{type(self).pages[path]}</main></body></html>"
            )
            self._send(200, body)
            return
        self._send(404, "not found", "text/plain")

    def _send(self, status: int, body: str, content_type: str = "text/html") -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@contextmanager
def collection_server() -> Iterator[tuple[str, type[CollectionFixtureHandler]]]:
    CollectionFixtureHandler.pages = {
        "/alpha": "Alpha version one",
        "/beta": "Beta stable",
        "/flaky": "Recovered source",
    }
    CollectionFixtureHandler.flaky_failures = 0
    CollectionFixtureHandler.robots_calls = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), CollectionFixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", CollectionFixtureHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def durable_store(tmp_path: Path) -> tuple[Storage, DurableJobStore]:
    storage = Storage(tmp_path / "data")
    await storage.initialize()
    jobs = DurableJobStore(storage)
    await jobs.initialize()
    return storage, jobs


async def enqueue_and_claim(
    jobs: DurableJobStore,
    identity: str,
    *,
    application_id: str = "daily-app",
    max_attempts: int = 3,
    retry_backoff_seconds: float = 0,
    lease_seconds: float = 30,
) -> Any:
    await jobs.enqueue(
        job_id=f"job-{identity}",
        idempotency_key=identity,
        application_id=application_id,
        version=1,
        node_id="schedule",
        trigger_kind="schedule",
        local_date="2026-07-16",
        payload={"lease_seconds": lease_seconds},
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    claimed = await jobs.claim_next(
        worker_id="collector-worker",
        lease_seconds=lease_seconds,
        application_id=application_id,
    )
    assert claimed is not None
    return claimed


def collector_config(base_url: str, *, fail_on_source_error: bool = False) -> WebCollectionConfig:
    del base_url
    return WebCollectionConfig(
        sources=[],
        allowed_hosts=["127.0.0.1"],
        permission_basis="controlled local integration fixture",
        respect_robots=True,
        robots_failure_policy="deny",
        timeout_seconds=5,
        max_content_bytes=1024,
        max_sources=20,
        fail_on_source_error=fail_on_source_error,
    )


@pytest.mark.asyncio
async def test_durable_store_is_idempotent_restart_safe_and_fenced(tmp_path: Path) -> None:
    storage, jobs = await durable_store(tmp_path)
    available = datetime.now(timezone.utc) - timedelta(seconds=1)
    first = await jobs.enqueue(
        job_id="job-idempotent",
        idempotency_key="schedule:daily-app:1:schedule:2026-07-16",
        application_id="daily-app",
        version=1,
        node_id="schedule",
        trigger_kind="schedule",
        local_date="2026-07-16",
        payload={"lease_seconds": 30},
        max_attempts=3,
        retry_backoff_seconds=0,
        available_at=available,
    )
    duplicate = await jobs.enqueue(
        job_id="ignored-duplicate-id",
        idempotency_key=first.idempotency_key,
        application_id="daily-app",
        version=1,
        node_id="schedule",
        trigger_kind="schedule",
        local_date="2026-07-16",
        payload={"lease_seconds": 30},
        max_attempts=3,
        retry_backoff_seconds=0,
        available_at=available,
    )
    assert duplicate.id == first.id
    assert [event.event_type for event in await jobs.list_events(first.id)].count("job.enqueued") == 1
    with pytest.raises(DurableJobConflict, match="already bound"):
        await jobs.enqueue(
            job_id="payload-conflict",
            idempotency_key=first.idempotency_key,
            application_id="daily-app",
            version=1,
            node_id="schedule",
            trigger_kind="schedule",
            local_date="2026-07-16",
            payload={"lease_seconds": 60},
            max_attempts=3,
            retry_backoff_seconds=0,
            available_at=available,
        )

    claimed = await jobs.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claimed and claimed.attempt_count == 1
    with pytest.raises(DurableJobConflict, match="stale owner or version"):
        await jobs.checkpoint(
            claimed.id,
            worker_id="worker-a",
            lease_version=claimed.lease_version - 1,
            values={"unsafe": True},
        )
    checkpointed = await jobs.checkpoint(
        claimed.id,
        worker_id="worker-a",
        lease_version=claimed.lease_version,
        values={"completed_source_keys": ["alpha"]},
    )
    assert checkpointed.checkpoint == {"completed_source_keys": ["alpha"]}

    restarted = DurableJobStore(storage)
    await restarted.initialize()
    persisted = await restarted.get(claimed.id)
    assert persisted.checkpoint == checkpointed.checkpoint
    completed = await restarted.complete(
        persisted.id,
        worker_id="worker-a",
        lease_version=persisted.lease_version,
        result={"answer": "durable result"},
    )
    assert completed.status == "succeeded"
    attempts = await restarted.list_attempts(completed.id)
    assert [(item.attempt_number, item.status) for item in attempts] == [(1, "succeeded")]


@pytest.mark.asyncio
async def test_cross_instance_claim_is_atomic_and_terminal_failure_alerts(
    tmp_path: Path,
) -> None:
    storage, jobs = await durable_store(tmp_path)
    await jobs.enqueue(
        job_id="job-atomic-claim",
        idempotency_key="atomic-claim",
        application_id="daily-app",
        version=1,
        node_id="schedule",
        trigger_kind="schedule",
        local_date="2026-07-16",
        payload={"lease_seconds": 30},
        max_attempts=1,
        retry_backoff_seconds=0,
        available_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    peer = DurableJobStore(storage)
    await peer.initialize()

    claims = await asyncio.gather(
        jobs.claim_next(worker_id="worker-a", lease_seconds=30),
        peer.claim_next(worker_id="worker-b", lease_seconds=30),
    )
    winners = [item for item in claims if item is not None]
    assert len(winners) == 1
    claimed = winners[0]
    assert claimed.id == "job-atomic-claim"

    failed = await jobs.fail(
        claimed.id,
        worker_id=str(claimed.lease_owner),
        lease_version=claimed.lease_version,
        error="terminal fixture failure",
        retryable=True,
    )
    assert failed.status == "failed"
    assert failed.alert == {
        "code": "durable_job_failed",
        "severity": "error",
        "message": "terminal fixture failure",
        "attempts": 1,
        "created_at": failed.alert["created_at"],
    }


@pytest.mark.asyncio
async def test_controlled_collection_enforces_boundaries_and_tracks_change(tmp_path: Path) -> None:
    storage, jobs = await durable_store(tmp_path)
    harness = PlatformHarness(storage=storage, network_egress_policy="full")
    collector = ControlledWebCollector(jobs=jobs, harness=harness)

    with collection_server() as (base_url, handler):
        config = collector_config(base_url)
        first = await enqueue_and_claim(jobs, "collection-day-one")
        first_result = await collector.collect(
            config=config,
            sources=[
                f"{base_url}/alpha",
                f"{base_url}/beta?b=2&a=1",
                f"{base_url}/blocked",
                f"{base_url}/oversized",
            ],
            application_id="daily-app",
            run_id="run-day-one",
            job_context={
                "job_id": first.id,
                "worker_id": first.lease_owner,
                "lease_version": first.lease_version,
            },
        )
        assert [item["status"] for item in first_result["receipts"]] == [
            "new",
            "new",
            "denied",
            "oversized",
        ]
        assert handler.robots_calls == 1
        assert first_result["receipts"][1]["canonical_url"].endswith("/beta?a=1&b=2")
        assert "arbitrary sites" in first_result["excluded_claims"][0]
        first_receipt_page = await jobs.list_receipts(first.id, limit=1, offset=0)
        second_receipt_page = await jobs.list_receipts(first.id, limit=1, offset=1)
        assert len(first_receipt_page) == len(second_receipt_page) == 1
        assert first_receipt_page[0].source_key != second_receipt_page[0].source_key
        await jobs.complete(
            first.id,
            worker_id=str(first.lease_owner),
            lease_version=first.lease_version,
            result=first_result,
        )

        second = await enqueue_and_claim(jobs, "collection-day-two")
        second_result = await collector.collect(
            config=config,
            sources=[f"{base_url}/alpha", f"{base_url}/beta?b=2&a=1"],
            application_id="daily-app",
            run_id="run-day-two",
            job_context={
                "job_id": second.id,
                "worker_id": second.lease_owner,
                "lease_version": second.lease_version,
            },
        )
        assert [item["status"] for item in second_result["receipts"]] == [
            "unchanged",
            "unchanged",
        ]
        await jobs.complete(
            second.id,
            worker_id=str(second.lease_owner),
            lease_version=second.lease_version,
            result=second_result,
        )

        handler.pages["/alpha"] = "Alpha version two"
        third = await enqueue_and_claim(jobs, "collection-day-three")
        third_result = await collector.collect(
            config=config,
            sources=[f"{base_url}/alpha", f"{base_url}/beta?b=2&a=1"],
            application_id="daily-app",
            run_id="run-day-three",
            job_context={
                "job_id": third.id,
                "worker_id": third.lease_owner,
                "lease_version": third.lease_version,
            },
        )
        assert [item["status"] for item in third_result["receipts"]] == [
            "changed",
            "unchanged",
        ]
        digest = collector.render_digest(
            CollectionDigestConfig(collection={}, topic="Daily changes"),
            third_result,
            "Daily changes",
        )
        assert "# Daily changes" in digest["text"]
        assert f"{base_url}/alpha" in digest["text"]
        assert "controlled allowlisted-source collection" in digest["text"]

        fourth = await enqueue_and_claim(jobs, "collection-canonical-dedupe")
        deduplicated = await collector.collect(
            config=config,
            sources=[f"{base_url}/beta?b=2&a=1", f"{base_url}/beta?a=1&b=2"],
            application_id="daily-app",
            run_id="run-canonical-dedupe",
            job_context={
                "job_id": fourth.id,
                "worker_id": fourth.lease_owner,
                "lease_version": fourth.lease_version,
            },
        )
        assert [item["status"] for item in deduplicated["receipts"]] == [
            "unchanged",
            "resumed",
        ]
        assert len(await jobs.list_receipts(fourth.id)) == 1


@pytest.mark.asyncio
async def test_retry_resumes_completed_sources_from_receipts(tmp_path: Path) -> None:
    storage, jobs = await durable_store(tmp_path)
    collector = ControlledWebCollector(
        jobs=jobs,
        harness=PlatformHarness(storage=storage, network_egress_policy="full"),
    )

    with collection_server() as (base_url, handler):
        handler.flaky_failures = 1
        config = collector_config(base_url, fail_on_source_error=True)
        first = await enqueue_and_claim(
            jobs,
            "retry-resume",
            max_attempts=2,
            retry_backoff_seconds=0,
        )
        with pytest.raises(RuntimeError, match="source collection failed"):
            await collector.collect(
                config=config,
                sources=[f"{base_url}/alpha", f"{base_url}/flaky"],
                application_id="daily-app",
                run_id="run-attempt-one",
                job_context={
                    "job_id": first.id,
                    "worker_id": first.lease_owner,
                    "lease_version": first.lease_version,
                },
            )
        retry_wait = await jobs.fail(
            first.id,
            worker_id=str(first.lease_owner),
            lease_version=first.lease_version,
            error="temporary fixture failure",
            retryable=True,
        )
        assert retry_wait.status == "retry_wait"

        second = await jobs.claim_next(
            worker_id="collector-worker",
            lease_seconds=30,
            application_id="daily-app",
        )
        assert second and second.attempt_count == 2
        resumed = await collector.collect(
            config=config,
            sources=[f"{base_url}/alpha", f"{base_url}/flaky"],
            application_id="daily-app",
            run_id="run-attempt-two",
            job_context={
                "job_id": second.id,
                "worker_id": second.lease_owner,
                "lease_version": second.lease_version,
            },
        )
        assert [item["status"] for item in resumed["receipts"]] == ["resumed", "new"]
        finished = await jobs.complete(
            second.id,
            worker_id=str(second.lease_owner),
            lease_version=second.lease_version,
            result=resumed,
        )
        assert finished.status == "succeeded"
        assert [item.status for item in await jobs.list_attempts(finished.id)] == [
            "failed",
            "succeeded",
        ]
        assert "collection.source_resumed" in {
            item.event_type for item in await jobs.list_events(finished.id)
        }


@pytest.mark.asyncio
async def test_retry_never_promotes_denied_receipt_to_resumed_success(tmp_path: Path) -> None:
    storage, jobs = await durable_store(tmp_path)
    collector = ControlledWebCollector(
        jobs=jobs,
        harness=PlatformHarness(storage=storage, network_egress_policy="full"),
    )

    with collection_server() as (base_url, _):
        config = collector_config(base_url, fail_on_source_error=True)
        claimed = await enqueue_and_claim(jobs, "denied-retry-boundary")
        context = {
            "job_id": claimed.id,
            "worker_id": claimed.lease_owner,
            "lease_version": claimed.lease_version,
        }
        for _ in range(2):
            with pytest.raises(RuntimeError, match="source collection denied"):
                await collector.collect(
                    config=config,
                    sources=[f"{base_url}/blocked"],
                    application_id="daily-app",
                    run_id="run-denied-retry",
                    job_context=context,
                )

        receipts = await jobs.list_receipts(claimed.id)
        assert len(receipts) == 1
        assert receipts[0].status == "denied"
        event_types = [item.event_type for item in await jobs.list_events(claimed.id)]
        assert "collection.source_problem_reused" in event_types
        assert "collection.source_resumed" not in event_types


@pytest.mark.asyncio
async def test_expired_lease_recovery_and_revision_guard(tmp_path: Path) -> None:
    _, jobs = await durable_store(tmp_path)
    claimed = await enqueue_and_claim(
        jobs,
        "lease-recovery",
        max_attempts=2,
        retry_backoff_seconds=0,
        lease_seconds=0.01,
    )
    await asyncio.sleep(0.03)
    recovered = await jobs.recover_expired(claimed.id)
    assert recovered.status == "retry_wait"
    assert recovered.lease_version > claimed.lease_version
    with pytest.raises(DurableJobConflict):
        await jobs.checkpoint(
            claimed.id,
            worker_id=str(claimed.lease_owner),
            lease_version=claimed.lease_version,
            values={"stale": True},
        )
    with pytest.raises(DurableJobConflict, match="revision conflict"):
        await jobs.resume(recovered.id, expected_revision=recovered.revision - 1)
    queued = await jobs.resume(recovered.id, expected_revision=recovered.revision)
    second = await jobs.claim_next(
        worker_id="collector-worker",
        lease_seconds=30,
        application_id="daily-app",
    )
    assert second and second.id == queued.id
    cancel_requested = await jobs.request_cancel(
        second.id,
        expected_revision=second.revision,
    )
    assert cancel_requested.cancel_requested is True
    cancelled = await jobs.cancel_terminal(
        second.id,
        worker_id=str(second.lease_owner),
        lease_version=second.lease_version,
        reason="operator cancelled the recovered attempt",
    )
    assert cancelled.status == "cancelled"
    assert [item.status for item in await jobs.list_attempts(cancelled.id)] == [
        "failed",
        "cancelled",
    ]


def settings(tmp_path: Path) -> Settings:
    config = Settings(
        api_token="durable-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    return config


def create_daily_application(client: TestClient) -> tuple[str, dict[str, Any]]:
    application = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": "Controlled daily digest",
            "requirement": "Collect declared sources every day and produce a traceable digest.",
        },
    )
    assert application.status_code == 201, application.text
    application_id = application.json()["id"]
    draft = client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()
    applied = client.post(
        f"/api/v1/applications/{application_id}/scenarios/daily_web_collection/apply",
        headers=HEADERS,
        json={
            "expected_revision": draft["revision"],
            "expected_content_hash": draft["content_hash"],
            "idempotency_key": "apply-daily-collection-scenario",
        },
    )
    assert applied.status_code == 200, applied.text
    return application_id, client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()


def wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for _ in range(300):
        response = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] in {"succeeded", "failed", "cancelled", "paused"}:
            return result
        time.sleep(0.01)
    return result


def test_daily_scenario_runs_as_durable_job_and_is_governable(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), NoopProvider())
    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/applications/missing/durable-jobs")
        assert unauthorized.status_code == 401

        catalog = client.get("/api/v1/scenarios", headers=HEADERS).json()
        daily = next(item for item in catalog if item["id"] == "daily_web_collection")
        assert daily["evidence_profile"]["selected_level"] == "H3"
        assert daily["evidence_profile"]["status"] == "integration_verified"
        assert "production unattended reliability or SLO" in daily["evidence_profile"][
            "excluded_claims"
        ]

        application_id, draft = create_daily_application(client)
        validation = client.post(
            f"/api/v1/applications/{application_id}/draft/validate",
            headers=HEADERS,
        )
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True, validation.json()
        node_types = {item["type"] for item in draft["snapshot"]["workflow"]["nodes"]}
        assert node_types == {
            "schedule_trigger",
            "web_collection",
            "collection_digest",
            "answer",
        }
        schedule = next(
            item
            for item in draft["snapshot"]["workflow"]["nodes"]
            if item["type"] == "schedule_trigger"
        )
        assert schedule["config"]["durable"] is True

        tests = client.post(
            f"/api/v1/applications/{application_id}/tests/run",
            headers=HEADERS,
        )
        assert tests.status_code == 200, tests.text
        assert tests.json()["passed"] is True, tests.json()
        published = client.post(
            f"/api/v1/applications/{application_id}/versions",
            headers=HEADERS,
            json={"acknowledge_warnings": True},
        )
        assert published.status_code == 200, published.text

        trigger_body = {
            "inputs": {},
            "idempotency_key": "manual-durable-e2e-2026-07-16",
        }
        triggered = client.post(
            f"/api/v1/applications/{application_id}/schedules/trigger",
            headers=HEADERS,
            json=trigger_body,
        )
        assert triggered.status_code == 202, triggered.text
        job_id = triggered.json()["job_id"]
        run_id = triggered.json()["run_id"]
        assert run_id
        run = wait_for_run(client, run_id)
        assert run["status"] == "succeeded", run
        client.portal.call(client.app.state.services.scheduler.reconcile_durable_jobs)

        exact = client.get(f"/api/v1/durable-jobs/{job_id}", headers=HEADERS)
        assert exact.status_code == 200, exact.text
        job = exact.json()
        assert job["status"] == "succeeded"
        assert len(job["attempts"]) == 1
        assert job["result"]["answer"].startswith("# Daily source digest")
        assert job["result"]["outputs"]["answer"] == job["result"]["answer"]

        duplicate = client.post(
            f"/api/v1/applications/{application_id}/schedules/trigger",
            headers=HEADERS,
            json=trigger_body,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == job_id
        assert len(
            client.get(
                f"/api/v1/applications/{application_id}/durable-jobs",
                headers=HEADERS,
            ).json()
        ) == 1

        schedule_status = client.get(
            f"/api/v1/applications/{application_id}/schedule-status",
            headers=HEADERS,
        ).json()
        assert schedule_status["schedule"]["durable"] is True
        assert schedule_status["latest_job"]["id"] == job_id

        governance = client.get(
            "/api/v1/governance/durable-jobs",
            headers=HEADERS,
            params={"application_id": application_id},
        )
        assert governance.status_code == 200, governance.text
        assert governance.json()["items"][0]["id"] == job_id
        assert governance.json()["support"]["production_slo"] == "unsupported"

        trace = client.get(
            f"/api/v1/governance/traces/{job['platform_task_id']}",
            headers=HEADERS,
        )
        assert trace.status_code == 200, trace.text
        assert trace.json()["durable_job"]["job"]["id"] == job_id
        assert trace.json()["durable_job"]["attempts"][0]["status"] == "succeeded"

        stale_cancel = client.post(
            f"/api/v1/durable-jobs/{job_id}/cancel",
            headers=HEADERS,
            json={"expected_revision": job["revision"]},
        )
        assert stale_cancel.status_code == 409

        second_trigger = client.post(
            f"/api/v1/applications/{application_id}/schedules/trigger",
            headers=HEADERS,
            json={
                "inputs": {},
                "idempotency_key": "manual-durable-e2e-page-two-2026-07-16",
            },
        )
        assert second_trigger.status_code == 202, second_trigger.text
        second_job_id = second_trigger.json()["job_id"]
        second_run_id = second_trigger.json()["run_id"]
        assert wait_for_run(client, second_run_id)["status"] == "succeeded"
        client.portal.call(client.app.state.services.scheduler.reconcile_durable_jobs)

        job_pages = [
            client.get(
                f"/api/v1/applications/{application_id}/durable-jobs",
                headers=HEADERS,
                params={"limit": 1, "offset": offset},
            ).json()
            for offset in (0, 1)
        ]
        assert all(len(page) == 1 for page in job_pages)
        assert {page[0]["id"] for page in job_pages} == {job_id, second_job_id}

        event_pages = [
            client.get(
                f"/api/v1/durable-jobs/{job_id}/events",
                headers=HEADERS,
                params={"limit": 1, "offset": offset},
            ).json()
            for offset in (0, 1)
        ]
        assert all(len(page) == 1 for page in event_pages)
        assert event_pages[0][0]["sequence"] != event_pages[1][0]["sequence"]

        governance_pages = [
            client.get(
                "/api/v1/governance/durable-jobs",
                headers=HEADERS,
                params={"application_id": application_id, "limit": 1, "offset": offset},
            ).json()
            for offset in (0, 1)
        ]
        assert [page["offset"] for page in governance_pages] == [0, 1]
        assert {page["items"][0]["id"] for page in governance_pages} == {
            job_id,
            second_job_id,
        }


def test_daily_scenario_evaluation_caps_claims_at_local_h3(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), NoopProvider())
    with TestClient(app) as client:
        application_id, draft = create_daily_application(client)
        h1 = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={"profile_id": "h1_static", "environment_id": "local_mock"},
        )
        assert h1.status_code == 200, h1.text
        assert h1.json()["achieved_status"] == "static_verified"
        assert h1.json()["executed_test_ids"] == []

        plan = client.post(
            f"/api/v1/applications/{application_id}/evaluation/plan",
            headers=HEADERS,
            json={"profile_id": "h3_integration", "environment_id": "local_contract"},
        )
        assert plan.status_code == 200, plan.text
        assert plan.json()["eligibility"] == "ready"
        assert {
            "durable_schedule",
            "retry_resume",
            "audit_provenance",
            "site_access_contract",
        } <= {item["family"] for item in plan.json()["cases"]}
        node_types = {item["type"] for item in draft["snapshot"]["workflow"]["nodes"]}
        for case in plan.json()["cases"]:
            assert set(case["test"]["required_node_types"]) <= node_types, case
        requirements_by_capability = {
            item["capability_ids"][0]: item["test"]["required_node_types"]
            for item in plan.json()["cases"]
        }
        assert requirements_by_capability["F.collect_sources"] == ["web_collection"]
        assert requirements_by_capability["G.retry_resume_dedupe"] == ["web_collection"]
        assert requirements_by_capability["G.provenance"] == ["web_collection"]
        assert requirements_by_capability["X.site_access"] == ["web_collection"]

        applied = client.post(
            f"/api/v1/applications/{application_id}/evaluation/tests/apply",
            headers=HEADERS,
            json={
                "profile_id": "h3_integration",
                "environment_id": "local_contract",
                "expected_revision": draft["revision"],
                "expected_content_hash": draft["content_hash"],
                "mode": "replace_generated",
                "idempotency_key": str(uuid4()),
            },
        )
        assert applied.status_code == 200, applied.text
        current = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        h3 = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={
                "profile_id": "h3_integration",
                "environment_id": "local_contract",
                "expected_revision": current["revision"],
                "expected_content_hash": current["content_hash"],
            },
        )
        assert h3.status_code == 200, h3.text
        assert h3.json()["outcome"] == "completed"
        assert h3.json()["achieved_status"] == "integration_verified"
        assert h3.json()["passed"] is True
        assert "production unattended reliability or SLO" in h3.json()["excluded_claims"]

        h4 = client.post(
            f"/api/v1/applications/{application_id}/evaluation/runs",
            headers=HEADERS,
            json={"profile_id": "h4_live", "environment_id": "configured_live"},
        )
        assert h4.status_code == 200
        assert h4.json()["outcome"] == "blocked"
        assert h4.json()["achieved_status"] == "blocked_by_environment"


def test_v049_contract_source_and_frontend_markers() -> None:
    contract = json.loads(
        (ROOT / "docs/evolution-control/stage-contracts/v0.4.9.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(contract["mandatory_tasks"]) == 7
    assert {item["task_id"] for item in contract["mandatory_tasks"]} == {
        f"V04-09-T01{suffix}" for suffix in "ABCDEFG"
    }

    registry = build_block_registry()
    workflow = registry.expand_template("daily_web_collection", prefix="daily")
    assert registry.validate_workflow(workflow) == []
    assert [item.type for item in workflow.nodes] == [
        "schedule_trigger",
        "web_collection",
        "collection_digest",
        "answer",
    ]

    durable_source = (
        ROOT / "platform/backend/src/agent_platform/durable_jobs.py"
    ).read_text(encoding="utf-8")
    collector_source = (
        ROOT / "platform/backend/src/agent_platform/web_collection.py"
    ).read_text(encoding="utf-8")
    api_source = (ROOT / "platform/backend/src/agent_platform/api.py").read_text(
        encoding="utf-8"
    )
    studio_source = (
        ROOT / "platform/frontend/app/applications/[id]/page.tsx"
    ).read_text(encoding="utf-8")
    runtime_source = (
        ROOT / "platform/frontend/app/runtime/[id]/page.tsx"
    ).read_text(encoding="utf-8")
    operations_source = (
        ROOT / "platform/frontend/app/schedule-operations-panel.tsx"
    ).read_text(encoding="utf-8")
    governance_source = (
        ROOT / "platform/frontend/app/governance/page.tsx"
    ).read_text(encoding="utf-8")

    for marker in (
        "idempotency_key",
        "lease_version",
        "job.checkpointed",
        "job.retry_scheduled",
        "collection_receipts",
    ):
        assert marker in durable_source
    for marker in (
        "allowed_hosts",
        "respect_robots",
        "permission_basis",
        "max_content_bytes",
        "previous_content_hash",
    ):
        assert marker in collector_source
    for route in (
        "/schedule-status",
        "/durable-jobs",
        "/retry",
        "/resume",
        "/cancel",
        "/api/v1/governance/durable-jobs",
    ):
        assert route in api_source
    assert "['build', 'edit', 'test', 'automation']" in studio_source
    assert 'data-studio-workspace="automation"' in studio_source
    assert "ScheduleOperationsPanel" in runtime_source
    assert "lease_owner" not in runtime_source
    assert 'data-customer-schedule-view="bounded"' in operations_source
    assert 'data-engineer-automation-workspace="true"' in operations_source
    assert "production SLO" in operations_source
    assert "Durable Jobs" in governance_source
