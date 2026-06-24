from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.workflow_runtime import WorkflowRuntime
from tests.test_runtime import ScriptedProvider


class IncrementalBuilderProvider(ModelProvider):
    name = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

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
        operations = [
            ("draft_add_node", {"node": {"id": "start", "type": "start", "title": "Input", "config": {"inputs": [{"name": "name", "type": "string"}]}}}),
            ("draft_add_node", {"node": {"id": "template", "type": "template_transform", "title": "Greeting", "config": {"template": "Hello {{ name }}", "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}}}}}),
            ("draft_add_node", {"node": {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"greeting": {"$ref": {"node_id": "template", "path": ["text"]}}}}}}),
            ("draft_connect", {"edge": {"id": "a", "source": "start", "target": "template", "source_port": "output", "target_port": "input"}}),
            ("draft_connect", {"edge": {"id": "b", "source": "template", "target": "end", "source_port": "text", "target_port": "input"}}),
            ("test_add", {"test": {"name": "Greets", "requirement": "Greeting contains name", "inputs": {"name": "Ada"}, "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}]}}),
            ("draft_validate", {}),
            ("test_run", {}),
            ("draft_publish", {}),
        ]
        name, value = operations[min(self.calls, len(operations) - 1)]
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "tool_use", "id": f"call-{self.calls}", "name": name, "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def mutate(client: TestClient, app_id: str, revision: int, op: str, data: dict) -> int:
    response = client.post(
        f"/api/v1/applications/{app_id}/draft",
        headers=headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def test_citation_gate_requires_every_output_url_to_come_from_tool_evidence() -> None:
    evidence = WorkflowRuntime._extract_urls(
        '{"results":[{"url":"https://news.example/one"},{"url":"https://news.example/two"}]}'
    )
    valid_output = WorkflowRuntime._extract_urls(
        {"report": "[one](https://news.example/one) and https://news.example/two"}
    )
    corrupted_output = WorkflowRuntime._extract_urls(
        {"report": "[one](https://news.example/one) and https://news.example/tw0"}
    )

    assert valid_output
    assert valid_output <= evidence
    assert corrupted_output - evidence == {"https://news.example/tw0"}


def test_incremental_workflow_test_publish_restore(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={
                "name": "Greeting workflow",
                "description": "A real editable workflow",
                "requirement": "Return a greeting containing the supplied name.",
                "mode": "workflow",
            },
        )
        assert created.status_code == 201, created.text
        app_id = created.json()["id"]
        revision = 0
        revision = mutate(client, app_id, revision, "add_node", {"node": {
            "id": "start", "type": "start", "title": "Input", "position": {"x": 0, "y": 0},
            "config": {"inputs": [{"name": "name", "label": "Name", "type": "string"}]},
        }})
        revision = mutate(client, app_id, revision, "add_node", {"node": {
            "id": "template", "type": "template_transform", "title": "Greeting",
            "position": {"x": 300, "y": 0},
            "config": {
                "template": "Hello, {{ name }}!",
                "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
            },
        }})
        revision = mutate(client, app_id, revision, "add_node", {"node": {
            "id": "end", "type": "end", "title": "Output", "position": {"x": 600, "y": 0},
            "config": {"outputs": {"greeting": {"$ref": {"node_id": "template", "path": ["text"]}}}},
        }})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "start-template", "source": "start", "target": "template",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "template-end", "source": "template", "target": "end",
            "source_port": "text", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Greets Ada",
            "requirement": "The output must greet the supplied name.",
            "inputs": {"name": "Ada"},
            "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello, Ada!"}],
            "mandatory": True,
        }})

        validation = client.post(
            f"/api/v1/applications/{app_id}/draft/validate", headers=headers()
        )
        assert validation.json()["valid"] is True, validation.text
        tested = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert tested.status_code == 200, tested.text
        assert tested.json()["passed"] is True, tested.text
        published = client.post(f"/api/v1/applications/{app_id}/versions", headers=headers())
        assert published.status_code == 200, published.text
        assert published.json()["version"] == 1
        platform_tools = client.get("/api/v1/tools", headers=headers()).json()
        assert any(item["name"] == f"workflow:{app_id}" for item in platform_tools)

        run = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {"name": "Grace"}, "workspace_path": "."},
        )
        assert run.status_code == 202, run.text
        run_id = run.json()["run_id"]
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert record["status"] == "succeeded", record
        assert record["outputs"] == {"greeting": "Hello, Grace!"}

        restored = client.post(
            f"/api/v1/applications/{app_id}/versions/1/restore", headers=headers()
        )
        assert restored.status_code == 200
        assert restored.json()["revision"] == revision + 1
        republish = client.post(f"/api/v1/applications/{app_id}/versions", headers=headers())
        assert republish.status_code == 409


def test_human_input_pauses_and_resumes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Approval", "requirement": "Ask a human to approve a request."},
        ).json()
        app_id, revision = created["id"], 0
        for node in [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "human", "type": "human_input", "title": "Approval", "config": {
                "title": "Approve?", "fields": [{"name": "approved", "label": "Approved", "type": "boolean"}]
            }},
            {"id": "end", "type": "end", "title": "End", "config": {
                "outputs": {"approved": {"$ref": {"node_id": "human", "path": ["approved"]}}}
            }},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "human", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "human", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Approval resume", "requirement": "Human approval reaches the output.",
            "inputs": {"__human__": {"human": {"approved": True}}},
            "assertions": [{"path": ["approved"], "operator": "equals", "expected": True}],
        }})
        assert client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers()).json()["passed"]
        assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200
        run_id = client.post(
            f"/api/v1/applications/{app_id}/runs", headers=headers(), json={"inputs": {}}
        ).json()["run_id"]
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] == "paused":
                break
            time.sleep(0.01)
        assert record["status"] == "paused"

    # A fresh FastAPI/WorkflowRuntime instance must recover the serialized graph
    # state and continue after the process that created the run has gone away.
    restarted = create_app(settings, ScriptedProvider())
    with TestClient(restarted) as client:
        resumed = client.post(
            f"/api/v1/runs/{run_id}/resume", headers=headers(), json={"values": {"approved": True}}
        )
        assert resumed.status_code == 200, resumed.text
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if record["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert record["outputs"] == {"approved": True}


def test_builder_uses_incremental_brick_operations_and_publishes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = IncrementalBuilderProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Generated", "requirement": "Build a tested greeting workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Build a tested greeting workflow.", "auto_publish": True},
        ).json()["build_id"]
        for _ in range(300):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"published", "needs_attention"}:
                break
            time.sleep(0.01)
        assert build["status"] == "published", build
        assert build["team_state"]["published_version"] == 1
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert [node["type"] for node in draft["snapshot"]["workflow"]["nodes"]] == [
            "start", "template_transform", "end"
        ]
        operation_events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        tools = [event["data"].get("tool") for event in operation_events if event["type"] == "build.operation"]
        assert tools == [name for name, _ in [
            ("draft_add_node", {}), ("draft_add_node", {}), ("draft_add_node", {}),
            ("draft_connect", {}), ("draft_connect", {}), ("test_add", {}),
            ("draft_validate", {}), ("test_run", {}), ("draft_publish", {}),
        ]]


def test_iteration_and_loop_execute_nested_workflows(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Containers", "requirement": "Map items and loop to an explicit exit."},
        ).json()["id"]
        nested_iteration = {
            "nodes": [
                {"id": "nested-start", "type": "start", "title": "Item", "config": {"inputs": [
                    {"name": "item", "type": "string"}, {"name": "index", "type": "number"}
                ]}},
                {"id": "nested-template", "type": "template_transform", "title": "Render", "config": {
                    "template": "Item {{ value }}", "variables": {
                        "value": {"$ref": {"node_id": "nested-start", "path": ["item"]}}
                    }
                }},
                {"id": "nested-end", "type": "end", "title": "Item output", "config": {
                    "outputs": {"value": {"$ref": {"node_id": "nested-template", "path": ["text"]}}}
                }},
            ],
            "edges": [
                {"id": "na", "source": "nested-start", "target": "nested-template", "source_port": "output", "target_port": "input"},
                {"id": "nb", "source": "nested-template", "target": "nested-end", "source_port": "text", "target_port": "input"},
            ],
        }
        nested_loop = {
            "nodes": [
                {"id": "loop-start", "type": "start", "title": "Iteration", "config": {"inputs": [
                    {"name": "iteration", "type": "number"}
                ]}},
                {"id": "loop-end", "type": "end", "title": "Counter", "config": {"outputs": {
                    "current": {"$ref": {"node_id": "loop-start", "path": ["iteration"]}}
                }}},
            ],
            "edges": [{"id": "lc", "source": "loop-start", "target": "loop-end", "source_port": "output", "target_port": "input"}],
        }
        nodes = [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [{"name": "items", "type": "array"}]}},
            {"id": "iteration", "type": "iteration", "title": "Map", "config": {
                "items": {"$ref": {"node_id": "start", "path": ["items"]}},
                "workflow": nested_iteration, "item_name": "item", "output_node_id": "nested-end",
                "output_path": ["value"], "parallelism": 2,
            }},
            {"id": "loop", "type": "loop", "title": "Count", "config": {
                "workflow": nested_loop, "variables": {},
                "break_condition": {"value": 0, "operator": "gte", "expected": 2},
                "break_value": {"$ref": {"node_id": "loop-end", "path": ["current"]}},
                "max_iterations": 5, "output_node_id": "loop-end",
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "mapped": {"$ref": {"node_id": "iteration", "path": ["items"]}},
                "counter": {"$ref": {"node_id": "loop", "path": ["output", "current"]}},
            }}},
        ]
        revision = 0
        for node in nodes:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "a", "source": "start", "target": "iteration", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "iteration", "target": "loop", "source_port": "items", "target_port": "input"},
            {"id": "c", "source": "loop", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Containers execute", "requirement": "Iteration maps and Loop exits.",
            "inputs": {"items": ["a", "b"]},
            "assertions": [
                {"path": ["mapped"], "operator": "equals", "expected": ["Item a", "Item b"]},
                {"path": ["counter"], "operator": "equals", "expected": 2},
            ],
        }})
        result = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert result.status_code == 200, result.text
        assert result.json()["passed"] is True, result.text


def test_branch_outputs_join_with_variable_aggregator(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Branch join", "requirement": "Join exactly one active branch."},
        ).json()["id"]
        nodes = [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": [{"name": "flag", "type": "boolean"}]}},
            {"id": "branch", "type": "if_else", "title": "Branch", "config": {
                "cases": [{"id": "yes", "conditions": [{"value": {"$ref": {"node_id": "start", "path": ["flag"]}}, "operator": "equals", "expected": True}]}],
                "default_branch": "no",
            }},
            {"id": "yes", "type": "variable_assigner", "title": "Yes", "config": {"assignments": {"value": "approved"}}},
            {"id": "no", "type": "variable_assigner", "title": "No", "config": {"assignments": {"value": "rejected"}}},
            {"id": "join", "type": "variable_aggregator", "title": "Join", "config": {
                "variables": [
                    {"$ref": {"node_id": "yes", "path": ["output", "value"], "optional": True}},
                    {"$ref": {"node_id": "no", "path": ["output", "value"], "optional": True}},
                ], "mode": "first_non_null",
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "decision": {"$ref": {"node_id": "join", "path": ["output"]}}
            }}},
        ]
        revision = 0
        for node in nodes:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in [
            {"id": "s-b", "source": "start", "target": "branch", "source_port": "output", "target_port": "input"},
            {"id": "b-y", "source": "branch", "target": "yes", "source_port": "branch", "target_port": "input", "branch": "yes"},
            {"id": "b-n", "source": "branch", "target": "no", "source_port": "branch", "target_port": "input", "branch": "no"},
            {"id": "y-j", "source": "yes", "target": "join", "source_port": "output", "target_port": "input"},
            {"id": "n-j", "source": "no", "target": "join", "source_port": "output", "target_port": "input"},
            {"id": "j-e", "source": "join", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        for flag, expected in [(True, "approved"), (False, "rejected")]:
            revision = mutate(client, app_id, revision, "add_test", {"test": {
                "name": f"Decision {flag}", "requirement": "Only the active branch is returned.",
                "inputs": {"flag": flag},
                "assertions": [{"path": ["decision"], "operator": "equals", "expected": expected}],
            }})
        result = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert result.json()["passed"] is True, result.text


def test_daily_schedule_is_persisted_and_deduplicated(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "Daily", "requirement": "Run every day at 08:00 Tokyo time."},
        ).json()["id"]
        revision = 0
        for node in [
            {"id": "schedule", "type": "schedule_trigger", "title": "08:00 JST", "config": {
                "timezone": "Asia/Tokyo", "hour": 8, "minute": 0, "inputs": {"topic": "idols"}
            }},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {
                "topic": {"$ref": {"node_id": "schedule", "path": ["topic"]}}
            }}},
        ]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        revision = mutate(client, app_id, revision, "add_edge", {"edge": {
            "id": "scheduled-end", "source": "schedule", "target": "end",
            "source_port": "output", "target_port": "input",
        }})
        revision = mutate(client, app_id, revision, "add_test", {"test": {
            "name": "Scheduled inputs", "requirement": "Schedule defaults reach the result.",
            "inputs": {},
            "assertions": [{"path": ["topic"], "operator": "equals", "expected": "idols"}],
        }})
        assert client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers()).json()["passed"]
        assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200
        schedules = client.get("/api/v1/schedules", headers=headers()).json()
        assert schedules[0]["timezone"] == "Asia/Tokyo"
        assert schedules[0]["hour"] == 8

        scheduler = client.app.state.services.scheduler
        assert client.portal.call(
            scheduler.tick, datetime(2026, 6, 23, 22, 59, tzinfo=timezone.utc)
        ) == []
        started = client.portal.call(
            scheduler.tick, datetime(2026, 6, 23, 23, 0, tzinfo=timezone.utc)
        )
        assert len(started) == 1
        assert client.portal.call(
            scheduler.tick, datetime(2026, 6, 23, 23, 30, tzinfo=timezone.utc)
        ) == []
        run_id = started[0]["run_id"]
        for _ in range(100):
            run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
            if run["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert run["outputs"] == {"topic": "idols"}
