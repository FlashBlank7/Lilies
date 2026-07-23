from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition, Usage
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


HEADERS = {"Authorization": "Bearer governance-test", "Content-Type": "application/json"}
ROOT = Path(__file__).resolve().parents[1]


class MeteredProvider(ModelProvider):
    name = "metered-provider"

    def __init__(self, response_text: str = "done", *, usage_mode: str = "full") -> None:
        self.response_text = response_text
        self.usage_mode = usage_mode

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, True, False, 100_000, 10_000)

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
        start_usage: dict[str, int] = {}
        if self.usage_mode in {"full", "estimated", "partial"}:
            start_usage["input_tokens"] = 11
        if self.usage_mode in {"full", "estimated"}:
            start_usage.update({
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
            })
        yield StreamEvent(type="message_start", data={"message": {"usage": start_usage}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": self.response_text},
        })
        end_usage: dict[str, int | float] = {}
        if self.usage_mode == "full":
            end_usage = {
                "output_tokens": 7,
                "reasoning_tokens": 5,
                "cost_usd": 0.25,
            }
        elif self.usage_mode == "estimated":
            end_usage = {"output_tokens": 7, "reasoning_tokens": 5}
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"},
            "usage": end_usage,
        })


def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="governance-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        platform_harness_worker_lease_seconds=30,
    )


def test_provider_usage_support_and_cost_provenance(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), MeteredProvider())
    with TestClient(app) as client:
        runtime = client.app.state.services.runtime
        provider = client.app.state.services.provider
        response = client.portal.call(
            runtime._collect_stream,
            "usage-fixture",
            provider.stream(
                model="metered-v1",
                system="test",
                messages=[],
                tools=[],
                max_output_tokens=100,
                thinking_enabled=True,
                effort="low",
            ),
            "usage.fixture",
            "metered-v1",
        )

    assert response.usage.input_tokens == 11
    assert response.usage.output_tokens == 7
    assert response.usage.cache_read_input_tokens == 3
    assert response.usage.cache_creation_input_tokens == 2
    assert response.usage.reasoning_tokens == 5
    assert response.usage.cost_usd == 0.25
    assert response.usage.cost_source == "provider_reported"
    assert response.usage.field_support == {
        "input_tokens": "reported",
        "cache_read_input_tokens": "reported",
        "cache_creation_input_tokens": "reported",
        "output_tokens": "reported",
        "reasoning_tokens": "reported",
        "cost_usd": "reported",
    }


def test_estimated_partial_and_missing_provider_usage_remain_explicit(tmp_path: Path) -> None:
    config = settings(tmp_path)
    config.model_price_estimates_usd_per_million["metered-v1"] = {
        "input_tokens": 1.0,
        "output_tokens": 2.0,
    }
    estimated_app = create_app(config, MeteredProvider(usage_mode="estimated"))
    with TestClient(estimated_app) as client:
        runtime = client.app.state.services.runtime
        provider = client.app.state.services.provider
        estimated = client.portal.call(
            runtime._collect_stream,
            "estimated-usage",
            provider.stream(
                model="metered-v1",
                system="test",
                messages=[],
                tools=[],
                max_output_tokens=100,
                thinking_enabled=False,
                effort="low",
            ),
            "usage.estimated",
            "metered-v1",
        )
        assert estimated.usage.input_tokens == 11
        assert estimated.usage.output_tokens == 7
        assert estimated.usage.reasoning_tokens == 5
        assert estimated.usage.cost_usd == 0.000025
        assert estimated.usage.cost_source == "estimated_configured_price"
        assert estimated.usage.field_support["cost_usd"] == "estimated"

    partial_app = create_app(
        settings(tmp_path / "partial"),
        MeteredProvider(usage_mode="partial"),
    )
    with TestClient(partial_app) as client:
        runtime = client.app.state.services.runtime
        provider = client.app.state.services.provider
        partial = client.portal.call(
            runtime._collect_stream,
            "partial-usage",
            provider.stream(
                model="metered-v1",
                system="test",
                messages=[],
                tools=[],
                max_output_tokens=100,
                thinking_enabled=False,
                effort="low",
            ),
            "usage.partial",
            "metered-v1",
        )
        assert partial.usage.input_tokens == 11
        assert partial.usage.output_tokens == 0
        assert partial.usage.cost_source == "unsupported"
        assert partial.usage.field_support["input_tokens"] == "reported"
        assert partial.usage.field_support["cost_usd"] == "unsupported"

    unpriced_app = create_app(
        settings(tmp_path / "unpriced"),
        MeteredProvider(usage_mode="estimated"),
    )
    with TestClient(unpriced_app) as client:
        runtime = client.app.state.services.runtime
        provider = client.app.state.services.provider
        unpriced = client.portal.call(
            runtime._collect_stream,
            "unpriced-usage",
            provider.stream(
                model="unknown-v1",
                system="test",
                messages=[],
                tools=[],
                max_output_tokens=100,
                thinking_enabled=False,
                effort="low",
            ),
            "usage.unpriced",
            "unknown-v1",
        )
        assert unpriced.usage.input_tokens == 11
        assert unpriced.usage.output_tokens == 7
        assert unpriced.usage.cost_source == "unsupported"
        assert unpriced.usage.field_support["cost_usd"] == "unsupported"

    missing_app = create_app(settings(tmp_path / "missing"), MeteredProvider(usage_mode="none"))
    with TestClient(missing_app) as client:
        runtime = client.app.state.services.runtime
        provider = client.app.state.services.provider
        missing = client.portal.call(
            runtime._collect_stream,
            "missing-usage",
            provider.stream(
                model="metered-v1",
                system="test",
                messages=[],
                tools=[],
                max_output_tokens=100,
                thinking_enabled=False,
                effort="low",
            ),
            "usage.missing",
            "metered-v1",
        )
        assert missing.usage.input_tokens == 0
        assert missing.usage.output_tokens == 0
        assert missing.usage.reasoning_tokens is None
        assert missing.usage.cost_usd == 0.0
        assert missing.usage.cost_source == "unsupported"
        assert missing.usage.field_support == {"cost_usd": "unsupported"}


def test_governance_queries_persist_usage_filters_trace_and_budget(tmp_path: Path) -> None:
    config = settings(tmp_path)
    app = create_app(config, MeteredProvider())
    with TestClient(app) as client:
        harness = client.app.state.services.harness

        async def prepare() -> None:
            await harness.start_task(
                "build-parent",
                kind="builder_build",
                owner_id="app-a",
                resource_id="build-parent",
                metadata={
                    "application_id": "app-a",
                    "workflow_id": "workflow-a",
                    "model": "metered-v1",
                    "budget_limit_usd": 1.0,
                },
            )
            await harness.record_usage(
                "build-parent",
                "model_call",
                metadata={"model": "metered-v1"},
            )
            await harness.record_model_usage(
                "build-parent",
                Usage(
                    input_tokens=11,
                    output_tokens=7,
                    cache_read_input_tokens=3,
                    cache_creation_input_tokens=2,
                    reasoning_tokens=5,
                    cost_usd=0.25,
                    cost_source="provider_reported",
                    field_support={
                        "input_tokens": "reported",
                        "output_tokens": "reported",
                        "cache_read_input_tokens": "reported",
                        "cache_creation_input_tokens": "reported",
                        "reasoning_tokens": "reported",
                        "cost_usd": "reported",
                    },
                ),
                model="metered-v1",
                provider="metered-provider",
                metadata={
                    "application_id": "app-a",
                    "workflow_id": "workflow-a",
                    "phase": "test",
                },
            )
            await harness.start_task(
                "run-child",
                kind="workflow_run",
                owner_id="app-a",
                resource_id="run-child",
                parent_task_id="build-parent",
                metadata={"application_id": "app-a", "workflow_id": "workflow-a"},
            )
            await harness.finish_task("run-child", status="failed", error="tool timeout")
            await harness.finish_task("build-parent", status="succeeded")

        client.portal.call(prepare)

        tasks = client.get(
            "/api/v1/governance/tasks?application_id=app-a&limit=1",
            headers=HEADERS,
        )
        assert tasks.status_code == 200, tasks.text
        task_page = tasks.json()
        assert task_page["total"] == 2
        assert len(task_page["items"]) == 1
        assert task_page["has_more"] is True

        usage = client.get(
            "/api/v1/governance/usage?application_id=app-a&model=metered-v1",
            headers=HEADERS,
        )
        assert usage.status_code == 200, usage.text
        payload = usage.json()
        assert payload["sample_count"] == 1
        assert payload["totals"] == {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 2,
            "reasoning_tokens": 5,
            "cost_usd": 0.25,
            "cached_input_tokens": 5,
        }
        assert all(payload["support"][field] == "reported" for field in payload["support"])
        assert payload["budgets"][0]["remaining_usd"] == 0.75
        assert "model_call counts are excluded" in payload["token_boundary"]

        trace = client.get("/api/v1/governance/traces/run-child", headers=HEADERS)
        assert trace.status_code == 200, trace.text
        trace_payload = trace.json()
        assert trace_payload["root_task_id"] == "build-parent"
        assert trace_payload["ancestors"] == ["build-parent"]
        assert trace_payload["tree"]["children"][0]["id"] == "run-child"
        assert any(span["span_type"] == "model_usage" for span in trace_payload["spans"])

        overview = client.get("/api/v1/governance/overview?application_id=app-a", headers=HEADERS)
        assert overview.status_code == 200
        assert overview.json()["task_counts"]["failed"] == 1
        assert any(item["detector"] == "task_failed" for item in overview.json()["alerts"])

    restarted = create_app(config, MeteredProvider())
    with TestClient(restarted) as client:
        persisted = client.get(
            "/api/v1/governance/usage?application_id=app-a",
            headers=HEADERS,
        )
        assert persisted.status_code == 200
        assert persisted.json()["sample_count"] == 1
        assert persisted.json()["samples"][0]["cost_source"] == "provider_reported"


def test_governance_recomputes_budget_from_concurrent_usage_samples(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), MeteredProvider())
    with TestClient(app) as client:
        harness = client.app.state.services.harness

        async def prepare() -> None:
            await harness.start_task(
                "parallel-budget",
                kind="agent_generation",
                owner_id="budget-owner",
                resource_id="parallel-budget",
                metadata={"application_id": "budget-app", "budget_limit_usd": 0.5},
            )
            sample = Usage(
                input_tokens=11,
                output_tokens=7,
                cost_usd=0.25,
                cost_source="provider_reported",
                field_support={
                    "input_tokens": "reported",
                    "output_tokens": "reported",
                    "cost_usd": "reported",
                },
            )
            await asyncio.gather(*(
                harness.record_model_usage(
                    "parallel-budget",
                    sample,
                    model="metered-v1",
                    provider="metered-provider",
                )
                for _ in range(2)
            ))
            await harness.finish_task("parallel-budget", status="succeeded")

        client.portal.call(prepare)

        usage = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": "parallel-budget"},
        ).json()
        assert usage["sample_count"] == 2
        budget = usage["budgets"][0]
        assert budget["task_id"] == "parallel-budget"
        assert budget["sample_count"] == 2
        assert budget["limit_usd"] == 0.5
        assert budget["spent_usd"] == 0.5
        assert budget["remaining_usd"] == 0.0
        assert budget["exhausted"] is True
        assert budget["support"] == "reported_or_estimated"

        alerts = client.get(
            "/api/v1/governance/alerts",
            headers=HEADERS,
            params={"task_id": "parallel-budget"},
        ).json()
        assert any(item["detector"] == "budget_exhausted" for item in alerts["items"])


def test_governance_aggregates_beyond_task_and_sample_page_limits(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), MeteredProvider())
    with TestClient(app) as client:
        harness = client.app.state.services.harness

        async def prepare() -> None:
            for index in range(205):
                task_id = f"bulk-{index:03d}"
                await harness.start_task(
                    task_id,
                    kind="builder_build",
                    owner_id="bulk-app",
                    resource_id=task_id,
                    metadata={
                        "application_id": "bulk-app",
                        "workflow_id": "bulk-workflow",
                        "model": "metered-v1",
                    },
                )
                if index == 0:
                    for turn in range(3):
                        await harness.record_model_usage(
                            task_id,
                            Usage(
                                input_tokens=11,
                                output_tokens=7,
                                cache_read_input_tokens=3,
                                cache_creation_input_tokens=2,
                                reasoning_tokens=5,
                                cost_usd=0.25,
                                cost_source="provider_reported",
                                field_support={
                                    "input_tokens": "reported",
                                    "output_tokens": "reported",
                                    "cache_read_input_tokens": "reported",
                                    "cache_creation_input_tokens": "reported",
                                    "reasoning_tokens": "reported",
                                    "cost_usd": "reported",
                                },
                            ),
                            model="metered-v1",
                            provider="metered-provider",
                            metadata={"turn": turn + 1},
                        )
                await harness.finish_task(task_id, status="failed", error="bulk failure")

        client.portal.call(prepare)

        page = client.get(
            "/api/v1/governance/tasks",
            headers=HEADERS,
            params={"application_id": "bulk-app", "limit": 10},
        ).json()
        assert page["total"] == 205
        assert len(page["items"]) == 10
        assert page["has_more"] is True

        overview = client.get(
            "/api/v1/governance/overview",
            headers=HEADERS,
            params={"application_id": "bulk-app"},
        ).json()
        assert overview["task_counts"]["total"] == 205
        assert overview["task_counts"]["failed"] == 205

        reliability = client.get(
            "/api/v1/governance/reliability",
            headers=HEADERS,
            params={"application_id": "bulk-app"},
        ).json()
        assert reliability["queue"]["scope"] == "filtered_governance_tasks"
        assert reliability["queue"]["task_counts"]["failed"] == 205

        alerts = client.get(
            "/api/v1/governance/alerts",
            headers=HEADERS,
            params={"application_id": "bulk-app"},
        ).json()
        assert alerts["total"] == 205

        usage = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": "bulk-000", "limit": 2},
        ).json()
        assert usage["sample_count"] == 3
        assert usage["returned_sample_count"] == 2
        assert usage["has_more"] is True
        assert usage["totals"]["input_tokens"] == 33
        assert usage["totals"]["cached_input_tokens"] == 15
        assert usage["dimensions"]["model"][0]["tokens"] == 54
        assert usage["dimensions"]["model"][0]["cached_input_tokens"] == 15
        assert usage["dimensions"]["model"][0]["reasoning_tokens"] == 15

        unrelated = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": "bulk-001"},
        ).json()
        assert unrelated["sample_count"] == 0


def test_governance_reliability_policy_audit_and_capability_claims(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), MeteredProvider())
    with TestClient(app) as client:
        harness = client.app.state.services.harness

        async def prepare() -> None:
            await harness.record_worker_heartbeat(
                worker_id="worker-a",
                status="idle",
                stale_after_seconds=120,
            )
            await harness.start_task(
                "retry-task",
                kind="workflow_run",
                owner_id="app-b",
                resource_id="retry-task",
                metadata={"application_id": "app-b", "origin": "resume"},
            )
            await client.app.state.services.storage.append_event(
                "retry-task",
                "node.retry",
                {"attempt": 2},
            )
            await client.app.state.services.storage.append_event(
                "retry-task",
                "node.timeout",
                {"timeout_seconds": 1},
            )
            await harness.finish_task("retry-task", status="cancelled")
            await harness.start_task(
                "claimed-task",
                kind="scheduler_manual_trigger",
                owner_id="app-b",
                resource_id="claimed-task",
                metadata={"application_id": "app-b"},
                worker_id="producer",
                lease_seconds=30,
            )
            await harness.release_task_lease(
                "claimed-task",
                worker_id="producer",
                next_status="queued",
            )
            claimed = await harness.claim_next_queued_task(
                worker_id="worker-b",
                lease_seconds=30,
            )
            assert claimed is not None and claimed.id == "claimed-task"
            await asyncio.sleep(0.01)
            await harness.renew_task_lease(
                "claimed-task",
                worker_id="worker-b",
                lease_seconds=30,
            )
            await harness.finish_task("claimed-task", status="succeeded")

        client.portal.call(prepare)

        reliability = client.get(
            "/api/v1/governance/reliability?application_id=app-b",
            headers=HEADERS,
        )
        assert reliability.status_code == 200, reliability.text
        metrics = reliability.json()["metrics"]
        assert metrics["retries"] == 1
        assert metrics["timeouts"] == 1
        assert metrics["cancelled"] == 1
        assert metrics["resumed"] == 1
        assert reliability.json()["workers"][0]["worker_id"] == "worker-a"

        claimed_page = client.get(
            "/api/v1/governance/tasks?task_id=claimed-task",
            headers=HEADERS,
        ).json()
        assert claimed_page["total"] == 1
        claimed_task = claimed_page["items"][0]
        assert claimed_task["queue_delay_seconds"] is not None
        lease = claimed_task["metadata"]["worker_lease"]
        assert lease["queue_claimed_at"] != lease["updated_at"]

        update = client.patch(
            "/api/v1/platform/harness/policy-controls",
            headers=HEADERS,
            json={"network_egress_policy": "none", "reason": "governance test"},
        )
        assert update.status_code == 200, update.text
        policy = client.get("/api/v1/governance/policy", headers=HEADERS)
        assert policy.status_code == 200
        assert policy.json()["controls"]["network_egress_policy"] == "none"
        assert policy.json()["audit"][0]["data"]["audit"]["reason"] == "governance test"
        assert policy.json()["support"]["restart_persistence"] == "unsupported"

        evidence = client.get(
            "/api/v1/governance/capability-evidence",
            headers=HEADERS,
        )
        assert evidence.status_code == 200, evidence.text
        capabilities = {item["capability_id"]: item for item in evidence.json()["capabilities"]}
        assert "platform.task_durability" in capabilities
        assert "platform.model_usage_telemetry" in capabilities
        assert capabilities["platform.model_usage_telemetry"]["strongest_status"] in {
            "component_verified",
            "integration_verified",
        }
        assert evidence.json()["support"]["production_completeness"] == "unsupported"

        registry = client.app.state.services.templates.evidence
        original_integrity_errors = registry.integrity_errors
        registry.integrity_errors = lambda _record: ["forced integrity failure"]
        try:
            invalid = client.get(
                "/api/v1/governance/capability-evidence",
                headers=HEADERS,
            ).json()
        finally:
            registry.integrity_errors = original_integrity_errors
        assert all(
            item["strongest_status"] == "unverified"
            and item["evidence_level"] == "H0"
            and item["artifact_categories"] == []
            for item in invalid["capabilities"]
        )
        assert invalid["support"]["implementation"] == "not_recorded"


def test_real_model_entrypoints_emit_dimensional_usage(tmp_path: Path) -> None:
    intake_payload = {
        "status": "ready",
        "confidence": 0.93,
        "reasoning_summary": "The workflow boundary is sufficiently specified.",
        "detected_goal": "Turn a customer request into a governed workflow result.",
        "missing": [],
        "questions": [],
        "completed_requirement": "Build a governed workflow for an operations user.",
        "workflow_intent": {
            "target_user": "Operations user",
            "business_goal": "Process a customer request with traceable evidence.",
            "runtime_input": "Customer request",
            "core_steps": ["Understand the request", "Produce a result"],
            "runtime_output": "Readable result",
            "runtime_interface": "Single start action and result view",
            "permissions": ["Ask before external writes"],
            "acceptance_cases": ["A valid request returns a readable result"],
        },
    }
    intake_app = create_app(
        settings(tmp_path / "intake"),
        MeteredProvider(json.dumps(intake_payload)),
    )
    with TestClient(intake_app) as client:
        response = client.post(
            "/api/v1/requirements/complete",
            headers=HEADERS,
            json={"requirement": "Build a governed customer workflow.", "locale": "en"},
        )
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        usage = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": task_id},
        ).json()
        assert usage["sample_count"] == 1
        assert usage["samples"][0]["task_kind"] == "requirement_intake"
        assert usage["samples"][0]["phase"] == "requirement_intake"
        assert usage["samples"][0]["provider"] == "metered-provider"

    builder_config = settings(tmp_path / "builder")
    builder_config.complexity_router_default_mode = "disabled"
    builder_config.complexity_router_limited_default_enabled = False
    builder_app = create_app(builder_config, MeteredProvider())
    with TestClient(builder_app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Metered build", "requirement": "Build a workflow."},
        ).json()["id"]
        started = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={
                "requirement": "Build a workflow.",
                "auto_publish": False,
                "max_turns": 5,
                "max_repair_cycles": 1,
            },
        )
        assert started.status_code == 202, started.text
        build_id = started.json()["build_id"]
        for _ in range(200):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] not in {"queued", "building"}:
                break
            time.sleep(0.01)
        assert build["status"] == "needs_attention"
        usage = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": build_id},
        ).json()
        assert usage["sample_count"] == 1
        sample = usage["samples"][0]
        assert sample["task_kind"] == "builder_build"
        assert sample["application_id"] == application_id
        assert sample["workflow_id"] == application_id
        assert sample["phase"] == "builder_team"

    workflow_app = create_app(settings(tmp_path / "workflow"), MeteredProvider())
    with TestClient(workflow_app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Metered run", "requirement": "Summarize a customer note."},
        ).json()["id"]
        revision = 0
        nodes = [
            {
                "id": "start",
                "type": "start",
                "title": "Customer note",
                "config": {"inputs": [{"name": "note", "type": "string"}]},
            },
            {
                "id": "model",
                "type": "model_turn",
                "title": "Summarize",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["note"]}},
                    "settings": {"prompt": "Summarize the customer note."},
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "Result",
                "config": {"outputs": {"answer": {"$ref": {"node_id": "model", "path": ["text"]}}}},
            },
        ]
        for node in nodes:
            changed = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=HEADERS,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": "add_node",
                    "data": {"node": node},
                },
            )
            assert changed.status_code == 200, changed.text
            revision = changed.json()["revision"]
        for edge in [
            {"id": "start-model", "source": "start", "target": "model", "source_port": "output", "target_port": "input"},
            {"id": "model-end", "source": "model", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            changed = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=HEADERS,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": "add_edge",
                    "data": {"edge": edge},
                },
            )
            assert changed.status_code == 200, changed.text
            revision = changed.json()["revision"]
        started = client.post(
            f"/api/v1/applications/{application_id}/runs",
            headers=HEADERS,
            json={"inputs": {"note": "A concise customer note."}, "use_draft": True},
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["run_id"]
        for _ in range(200):
            run = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)
        assert run["status"] == "succeeded", run
        usage = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": run_id},
        ).json()
        assert usage["sample_count"] == 1
        sample = usage["samples"][0]
        assert sample["task_kind"] == "workflow_run"
        assert sample["application_id"] == application_id
        assert sample["workflow_id"] == application_id
        assert sample["phase"] == "workflow_model_text"
        assert sample["node_id"] == "model"


def test_deterministic_benchmark_and_repair_do_not_invent_token_usage(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), MeteredProvider())
    with TestClient(app) as client:
        graph = {
            "nodes": [
                {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
                {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
            ],
            "edges": [
                {"id": "start-end", "source": "start", "target": "end", "source_port": "output", "target_port": "input"},
            ],
        }
        benchmark = client.post(
            "/api/v1/builder-benchmark/evaluate",
            headers=HEADERS,
            json={
                "name": "deterministic-boundary",
                "reference": graph,
                "candidate": graph,
                "required_node_types": ["start", "end"],
            },
        )
        assert benchmark.status_code == 200, benchmark.text
        benchmark_usage = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": benchmark.json()["task_id"]},
        ).json()
        assert benchmark_usage["sample_count"] == 0
        assert benchmark_usage["totals"] == {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": None,
            "reasoning_tokens": None,
            "cost_usd": None,
            "cached_input_tokens": None,
        }

        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Repair boundary", "requirement": "Rename a workflow node."},
        ).json()["id"]
        revision = 0
        for node in graph["nodes"]:
            changed = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=HEADERS,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": "add_node",
                    "data": {"node": node},
                },
            )
            revision = changed.json()["revision"]
        repair = client.post(
            f"/api/v1/applications/{application_id}/draft/preview-patch",
            headers=HEADERS,
            json={"instruction": "rename node end to Final Answer"},
        )
        assert repair.status_code == 200, repair.text
        assert repair.json()["supported"] is True
        repair_usage = client.get(
            "/api/v1/governance/usage",
            headers=HEADERS,
            params={"task_id": repair.json()["task_id"]},
        ).json()
        assert repair_usage["sample_count"] == 0
        assert "model_call counts are excluded" in repair_usage["token_boundary"]


def test_customer_runtime_restores_latest_application_run(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), MeteredProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={
                "name": "Runtime history",
                "description": "Customer-visible deterministic workflow.",
                "requirement": "Accept a customer note and return a readable result.",
            },
        )
        assert created.status_code == 201, created.text
        application_id = created.json()["id"]
        revision = 0
        operations = [
            ("add_node", {"node": {
                "id": "start",
                "type": "start",
                "title": "Customer input",
                "config": {"inputs": [{"name": "note", "type": "string"}]},
            }}),
            ("add_node", {"node": {
                "id": "end",
                "type": "end",
                "title": "Customer result",
                "config": {"outputs": {"answer": "ready"}},
            }}),
            ("add_edge", {"edge": {
                "id": "start-end",
                "source": "start",
                "target": "end",
                "source_port": "output",
                "target_port": "input",
            }}),
        ]
        for operation, data in operations:
            changed = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=HEADERS,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": operation,
                    "data": data,
                },
            )
            assert changed.status_code == 200, changed.text
            revision = changed.json()["revision"]

        first = client.post(
            f"/api/v1/applications/{application_id}/runs",
            headers=HEADERS,
            json={"inputs": {"note": "first"}, "use_draft": True},
        )
        second = client.post(
            f"/api/v1/applications/{application_id}/runs",
            headers=HEADERS,
            json={"inputs": {"note": "second"}, "use_draft": True},
        )
        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text

        definition = client.get(
            f"/api/v1/applications/{application_id}/runtime-definition",
            headers=HEADERS,
        )
        assert definition.status_code == 200, definition.text
        assert definition.json()["source"] == "draft"
        assert definition.json()["version"] is None
        assert definition.json()["draft_revision"] == revision
        assert definition.json()["snapshot"]["workflow"]["nodes"][0]["id"] == "start"

        recent = client.get(
            f"/api/v1/applications/{application_id}/runs?limit=1",
            headers=HEADERS,
        )
        assert recent.status_code == 200, recent.text
        assert len(recent.json()) == 1
        assert recent.json()[0]["id"] == second.json()["run_id"]
        assert recent.json()[0]["state"]["inputs"] == {"note": "second"}

        missing = client.get(
            "/api/v1/applications/not-found/runs",
            headers=HEADERS,
        )
        assert missing.status_code == 404


def test_frontend_three_surface_contract_is_present() -> None:
    runtime = ROOT / "platform/frontend/app/runtime/[id]/page.tsx"
    governance = ROOT / "platform/frontend/app/governance/page.tsx"
    studio = ROOT / "platform/frontend/app/applications/[id]/page.tsx"
    markdown = ROOT / "platform/frontend/lib/markdown.tsx"
    assert runtime.is_file()
    assert governance.is_file()
    runtime_source = runtime.read_text(encoding="utf-8")
    governance_source = governance.read_text(encoding="utf-8")
    studio_source = studio.read_text(encoding="utf-8")
    markdown_source = markdown.read_text(encoding="utf-8")
    assert "data-customer-runtime" in runtime_source
    assert "data-governance-console" in governance_source
    assert "Customer Runtime" in runtime_source
    assert "准备任务信息" in runtime_source
    assert "理解并处理请求" in runtime_source
    assert "获取信息或执行操作" in runtime_source
    assert "确认安全边界" in runtime_source
    assert "href={`/applications/${id}`}" not in runtime_source
    assert "Trace Explorer" in governance_source
    assert "Cost & Tokens" in governance_source
    assert "Capability Evidence" in governance_source
    assert "offset=${taskOffset}" in governance_source
    assert "Previous task page" in governance_source
    assert "VISIBLE_STUDIO_TABS" in studio_source
    assert "['build', 'edit', 'test', 'automation']" in studio_source
    assert "router.replace(`/runtime/${id}`)" in studio_source
    assert "router.replace(`/governance?application_id=${id}`)" in studio_source
    assert "tab === 'run'" not in studio_source
    assert "tab === 'monitor'" not in studio_source
    assert "/api/v1/platform/harness/tasks" not in studio_source
    assert "/api/v1/customer-runtime/applications/${id}" in runtime_source
    assert "/api/v1/customer-runtime/runs/${runId}" in runtime_source
    assert "run?.state.snapshot || definition?.snapshot" in runtime_source
    assert "normalizeSerializedMarkdown" in markdown_source
    assert "hasEscapedBlockStructure" in markdown_source
