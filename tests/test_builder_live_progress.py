from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


class BatchedTaskProvider(ModelProvider):
    name = "batched-task-provider"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

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
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            tasks = [
                {"action": "create", "subject": "Understand the request"},
                {"action": "create", "subject": "Build and verify the workflow"},
            ]
            for index, task in enumerate(tasks):
                yield StreamEvent(type="content_block_start", data={
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"task-{index}",
                        "name": "task",
                        "input": {},
                    },
                })
                yield StreamEvent(type="content_block_delta", data={
                    "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(task)},
                })
                yield StreamEvent(type="content_block_stop", data={"index": index})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 1},
            })
            return
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": "done"},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": "done"},
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 1},
        })


class ProtectedDraftProvider(ModelProvider):
    name = "protected-draft-provider"

    def __init__(self) -> None:
        self.calls = 0
        self.system_prompts: list[str] = []
        self.last_user_texts: list[str] = []
        self.efforts: list[str] = []
        self.output_budgets: list[int] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

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
            ("task", {"action": "create", "subject": "Build a verified greeting"}),
            ("draft_add_node", {"node": {
                "id": "start", "type": "start", "title": "Input",
                "config": {"inputs": [{"name": "name", "type": "string"}]},
            }}),
            ("draft_add_node", {"node": {
                "id": "template", "type": "template_transform", "title": "Greeting",
                "config": {
                    "template": "Hello {{ name }}",
                    "variables": {"name": {"$ref": {"node_id": "start", "path": ["name"]}}},
                },
            }}),
            ("draft_add_node", {"node": {
                "id": "end", "type": "end", "title": "End",
                "config": {"outputs": {
                    "greeting": {"$ref": {"node_id": "template", "path": ["text"]}},
                }},
            }}),
            ("draft_connect", {"edge": {
                "id": "start-template", "source": "start", "target": "template",
                "source_port": "output", "target_port": "input",
            }}),
            ("draft_connect", {"edge": {
                "id": "template-end", "source": "template", "target": "end",
                "source_port": "text", "target_port": "input",
            }}),
            ("test_add", {"test": {
                "id": "greeting-test", "name": "Greeting exists",
                "requirement": "Return a greeting.", "inputs": {"name": "Ada"},
                "assertions": [{"path": ["greeting"], "operator": "equals", "expected": "Hello Ada"}],
                "mandatory": True,
            }}),
            ("test_run", {}),
            ("draft_add_node", {"node": {
                "id": "debug-node", "type": "template_transform", "title": "Temporary debug",
                "config": {"template": "debug", "variables": {}},
            }}),
            ("test_run", {}),
            ("draft_publish", {}),
        ]
        operation = operations[min(self.calls, len(operations) - 1)]
        self.calls += 1
        self.system_prompts.append(system)
        self.last_user_texts.append("".join(
            getattr(block, "text", "") or ""
            for block in messages[-1].content
            if getattr(block, "type", "") == "text"
        ))
        self.efforts.append(effort)
        self.output_budgets.append(max_output_tokens)
        name, value = operation
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {
                "type": "tool_use", "id": f"protected-{self.calls}", "name": name, "input": {},
            },
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


class StalledBuilderProvider(ModelProvider):
    name = "stalled-builder-provider"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

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
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {
                "type": "tool_use", "id": "inspect-only", "name": "catalog_search", "input": {},
            },
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"query":"start"}'},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


class DiscoveryThenTaskProvider(ModelProvider):
    name = "discovery-then-task-provider"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

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
        block_types = [
            "start",
            "question_classifier",
            "variable_assigner",
            "variable_aggregator",
            "template_transform",
            "end",
        ]
        self.calls += 1
        yield StreamEvent(type="message_start", data={
            "message": {"usage": {"input_tokens": 1}},
        })
        if self.calls <= len(block_types):
            name = "catalog_get"
            value = {"type": block_types[self.calls - 1]}
        elif self.calls == len(block_types) + 1:
            name = "task"
            value = {"action": "create", "subject": "Build after schema discovery"}
        else:
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {"type": "text", "text": ""},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "text_delta", "text": "ready to build"},
            })
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
            })
            return
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": f"discovery-{self.calls}",
                "name": name,
                "input": {},
            },
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
        })


class RestoreOnFailureProvider(ProtectedDraftProvider):
    name = "restore-on-failure-provider"

    async def stream(self, **kwargs: object) -> AsyncIterator[StreamEvent]:
        if self.calls < 7:
            async for event in super().stream(**kwargs):
                yield event
            return
        if self.calls == 7:
            self.calls += 1
            yield StreamEvent(type="message_start", data={
                "message": {"usage": {"input_tokens": 1}},
            })
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "invalidate-before-failure",
                    "name": "draft_add_node",
                    "input": {},
                },
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps({
                        "node": {
                            "id": "debug-node",
                            "type": "template_transform",
                            "title": "Temporary debug",
                            "config": {"template": "debug", "variables": {}},
                        },
                    }),
                },
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })
            return
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": "stopped mid-repair"},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "stopped mid-repair"},
        })
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
        })


class ConcurrentAcceptanceProvider(ModelProvider):
    name = "concurrent-acceptance-provider"

    def __init__(self) -> None:
        self.active_calls = 0
        self.max_active_calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

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
        self.active_calls += 1
        self.max_active_calls = max(self.max_active_calls, self.active_calls)
        try:
            yield StreamEvent(
                type="message_start",
                data={"message": {"usage": {"input_tokens": 1}}},
            )
            await asyncio.sleep(0.1)
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {"type": "text", "text": ""},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "text_delta", "text": "hello"},
            })
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
            })
        finally:
            self.active_calls -= 1


def mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    op: str,
    data: dict[str, object],
) -> int:
    response = client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def test_batched_builder_tasks_are_persisted_one_operation_at_a_time(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        ),
        BatchedTaskProvider(),
    )
    observed_task_counts: list[int] = []
    original_update = app.state.services.workflow_store.update_build

    async def tracking_update(build_id: str, **kwargs: object) -> None:
        team_state = kwargs.get("team_state")
        if team_state is not None:
            observed_task_counts.append(len(team_state.tasks))
        await original_update(build_id, **kwargs)

    app.state.services.workflow_store.update_build = tracking_update
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Live progress", "requirement": "Build a visible workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={
                "requirement": "Build a visible workflow with progress.",
                "auto_publish": False,
                "max_turns": 5,
            },
        ).json()["build_id"]
        for _ in range(200):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] == "needs_attention":
                break
            time.sleep(0.01)

        assert build["status"] == "needs_attention", build
        first_task = observed_task_counts.index(1)
        second_task = observed_task_counts.index(2)
        assert first_task < second_task
        events = client.get(f"/v1/streams/{build_id}", headers=HEADERS).json()
        task_events = [
            event for event in events
            if event["type"] == "build.operation" and event["data"].get("tool") == "task"
        ]
        assert [event["data"]["progress"]["task_count"] for event in task_events] == [1, 2]


def test_builder_preserves_valid_draft_and_completes_verified_task_ledger(tmp_path: Path) -> None:
    provider = ProtectedDraftProvider()
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        ),
        provider,
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Protected draft", "requirement": "Build a verified greeting workflow."},
        ).json()["id"]
        response = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={"requirement": "Build a verified greeting workflow.", "auto_publish": True},
        )
        assert response.status_code == 202, response.text
        created = response.json()
        build_id = created["build_id"]
        for _ in range(500):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] in {"published", "needs_attention"}:
                break
            time.sleep(0.01)

        assert build["status"] == "published", build
        assert build["max_turns"] == 36
        assert build["max_repair_cycles"] == 4
        assert build["max_elapsed_seconds"] == 480.0
        assert build["team_state"]["tasks"][0]["status"] == "completed"
        draft = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS,
        ).json()
        assert [node["id"] for node in draft["snapshot"]["workflow"]["nodes"]] == [
            "start", "template", "end",
        ]
        assert [test["id"] for test in draft["snapshot"]["tests"]] == ["greeting-test"]
        events = client.get(f"/v1/streams/{build_id}", headers=HEADERS).json()
        assert provider.calls == 8
        assert not any(
            event["type"] == "build.operation"
            and event["data"].get("tool") == "draft_add_node"
            and "debug-node" in event["data"].get("result", "")
            for event in events
        )
        assert any(event["type"] == "build.progress.completed" for event in events)
        assert any(event["type"] == "build.published" for event in events)
        # 缓存纪律：system 全常量，每轮遥测在末尾 user 消息里（前缀稳定才有缓存命中）
        assert "Current delivery budget" not in provider.system_prompts[0]
        assert provider.system_prompts[0] == provider.system_prompts[-1]
        assert "Current delivery budget: turn 1/36" in provider.last_user_texts[0]
        assert set(provider.efforts) == {"high"}
        assert set(provider.output_budgets) == {8_192}


def test_builder_stops_after_repeated_turns_without_durable_progress(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        ),
        StalledBuilderProvider(),
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Stalled build", "requirement": "Build a tested workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={"requirement": "Build a tested workflow.", "auto_publish": False},
        ).json()["build_id"]
        for _ in range(500):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] == "needs_attention":
                break
            time.sleep(0.01)

        assert build["status"] == "needs_attention", build
        assert "builder progress stalled" in build["error"]
        events = client.get(f"/v1/streams/{build_id}", headers=HEADERS).json()
        stalled = [event for event in events if event["type"] == "build.progress.stalled"]
        assert stalled[0]["data"]["stalled_turns"] == 6


def test_builder_allows_bounded_novel_schema_discovery_before_delivery(tmp_path: Path) -> None:
    provider = DiscoveryThenTaskProvider()
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        ),
        provider,
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Bounded discovery", "requirement": "Inspect schemas, then build."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={
                "requirement": "Inspect the needed schemas, then build a tested workflow.",
                "auto_publish": False,
                "max_turns": 10,
            },
        ).json()["build_id"]
        for _ in range(500):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] in {"ready", "published", "needs_attention"}:
                break
            time.sleep(0.01)

        assert provider.calls >= 8
        assert len(build["team_state"]["tasks"]) == 1
        events = client.get(f"/v1/streams/{build_id}", headers=HEADERS).json()
        assert not any(event["type"] == "build.progress.stalled" for event in events)
        assert any(
            event["type"] == "build.operation"
            and event["data"].get("tool") == "task"
            and event["data"].get("success") is True
            for event in events
        )


def test_builder_preserves_partial_draft_when_a_build_stops_mid_repair(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        ),
        RestoreOnFailureProvider(),
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Restore draft", "requirement": "Build a verified greeting workflow."},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={"requirement": "Build a verified greeting workflow.", "auto_publish": False},
        ).json()["build_id"]
        for _ in range(500):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] == "needs_attention":
                break
            time.sleep(0.01)

        assert build["status"] == "needs_attention", build
        draft = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS,
        ).json()
        # The partial draft is preserved for inspection and continuation —
        # a failed build no longer rolls the builder's work back.
        node_ids = [node["id"] for node in draft["snapshot"]["workflow"]["nodes"]]
        assert "debug-node" in node_ids
        events = client.get(f"/v1/streams/{build_id}", headers=HEADERS).json()
        assert not any(event["type"] == "build.draft.restored" for event in events)


def test_acceptance_suite_runs_independent_workflows_concurrently(tmp_path: Path) -> None:
    provider = ConcurrentAcceptanceProvider()
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        ),
        provider,
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Concurrent acceptance", "requirement": "Answer two test inputs."},
        ).json()["id"]
        revision = 0
        for node in [
            {
                "id": "start",
                "type": "start",
                "title": "Input",
                "config": {"inputs": [{"name": "prompt", "type": "string"}]},
            },
            {
                "id": "model",
                "type": "llm",
                "title": "Answer",
                "config": {
                    "prompt": {"$ref": {"node_id": "start", "path": ["prompt"]}},
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "Result",
                "config": {
                    "outputs": {
                        "answer": {"$ref": {"node_id": "model", "path": ["text"]}},
                    },
                },
            },
        ]:
            revision = mutate(client, application_id, revision, "add_node", {"node": node})
        for edge in [
            {
                "id": "start-model",
                "source": "start",
                "target": "model",
                "source_port": "output",
                "target_port": "input",
            },
            {
                "id": "model-end",
                "source": "model",
                "target": "end",
                "source_port": "text",
                "target_port": "input",
            },
        ]:
            revision = mutate(client, application_id, revision, "add_edge", {"edge": edge})
        for index in range(2):
            revision = mutate(client, application_id, revision, "add_test", {"test": {
                "id": f"parallel-{index}",
                "name": f"Parallel case {index}",
                "requirement": "The workflow returns the model answer.",
                "inputs": {"prompt": f"case {index}"},
                "assertions": [
                    {"path": ["answer"], "operator": "equals", "expected": "hello"},
                ],
                "mandatory": True,
            }})

        response = client.post(
            f"/api/v1/applications/{application_id}/tests/run",
            headers=HEADERS,
        )

        assert response.status_code == 200, response.text
        report = response.json()
        assert report["passed"] is True, report
        assert report["summary"]["passed"] == 2
        assert provider.max_active_calls >= 2
