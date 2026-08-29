from __future__ import annotations

from typing import Any

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
        # 高负载全量回归下 2s 窗口不够（多轮模型调用+落库），放宽到 30s；跑得快不多花时间
        for _ in range(3000):
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
        # 高负载全量回归下 5s 窗口不够，放宽到 30s
        for _ in range(3000):
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
        # 高负载全量回归下 5s 窗口不够，放宽到 30s
        for _ in range(3000):
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
        # 高负载全量回归下 5s 窗口不够，放宽到 30s
        for _ in range(3000):
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
        # 高负载全量回归下 5s 窗口不够，放宽到 30s
        for _ in range(3000):
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


def test_builder_cannot_publish_without_acceptance_evidence(tmp_path: Path) -> None:
    """验收证据硬门：构建者在测试未绿时调用 draft_publish 必须被拒。

    真实事故（2026-08-23）：某单在最后一次 test_run 明确 passed=false 之后，
    协调者以 draft_publish{explicit:true} 发布成功——平台层"只提示不阻断"的
    人类例外通道被自动化一方借用，产出的日报工作流把样例门店名硬编码进公式，
    换门店直接崩，却成了正式版 v1。业务验收可由业主知情越过，构建者不可以。
    """

    class PublishWithoutTestsProvider(ModelProvider):
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self, model: str) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

        async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            operations: list[tuple[str, dict[str, Any]]] = [
                ("draft_add_node", {"node": {
                    "id": "start", "type": "start", "title": "Input",
                    "config": {"inputs": [{"name": "name", "type": "string"}]},
                }}),
                ("draft_add_node", {"node": {
                    "id": "end", "type": "end", "title": "End",
                    "config": {"outputs": {"greeting": {"$ref": {
                        "node_id": "start", "path": ["name"]}}}},
                }}),
                ("draft_connect", {"edge": {
                    "id": "e1", "source": "start", "target": "end",
                    "source_port": "output", "target_port": "input",
                }}),
                # 强制测试注定失败：工作流只透传姓名，断言却要 "Hello Ada"
                ("test_add", {"test": {
                    "id": "greeting-test", "name": "问候语",
                    "requirement": "返回 Hello Ada", "inputs": {"name": "Ada"},
                    "assertions": [{"path": ["greeting"], "operator": "equals",
                                    "expected": "Hello Ada"}],
                    "mandatory": True,
                }}),
                ("test_run", {}),
                # 明知失败仍显式发布——事故当晚正是这条路径放行的
                ("draft_publish", {"explicit": True}),
                ("draft_publish", {"explicit": True}),
            ]
            name, value = operations[min(self.calls - 1, len(operations) - 1)]
            yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"c{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })

    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, PublishWithoutTestsProvider())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer workflow-test"}
        app_id = client.post("/api/v1/applications", headers=headers,
                             json={"name": "NoEvidence", "requirement": "验收证据硬门验证。"}).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers,
            json={"requirement": "输入姓名 name，输出 greeting 字段。",
                  "auto_publish": True, "max_turns": 8, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 绝不允许在零验收证据下发布
        assert build["status"] != "published", "零验收证据竟然发布成功"
        versions = client.get(f"/api/v1/applications/{app_id}/versions", headers=headers)
        if versions.status_code == 200:
            assert not (versions.json() or []), "不应存在任何已发布版本"
        events = client.get(f"/v1/streams/{build_id}", headers=headers).json()
        blocked = [
            e for e in events
            if e["type"] == "build.operation"
            and (e["data"] or {}).get("tool") == "draft_publish"
            and "publish blocked" in str((e["data"] or {}).get("result") or "")
        ]
        assert blocked, "draft_publish 应被证据硬门拒绝并留下记录"


def test_builder_cannot_publish_on_structural_only_acceptance(tmp_path: Path) -> None:
    """结构断言不算验收证据：只证明"跑得起来"，不证明"算得对"。

    平台在构建者没写验收时会自动补一条纯结构冒烟测试
    （operator=exists, structural=True）。它跑绿之后证据状态就是 current——
    于是零真实验收的工作流又能从这个入口发布出去，和 2026-08-23 那次假成功
    是同一个洞换了个门。业主可以知情越过业务验收，构建者不可以。
    """

    class StructuralOnlyProvider(ModelProvider):
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self, model: str) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

        async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            operations: list[tuple[str, dict[str, Any]]] = [
                ("draft_add_node", {"node": {
                    "id": "start", "type": "start", "title": "Input",
                    "config": {"inputs": [{"name": "name", "type": "string"}]},
                }}),
                ("draft_add_node", {"node": {
                    "id": "end", "type": "end", "title": "End",
                    "config": {"outputs": {"greeting": {"$ref": {
                        "node_id": "start", "path": ["name"]}}}},
                }}),
                ("draft_connect", {"edge": {
                    "id": "e1", "source": "start", "target": "end",
                    "source_port": "output", "target_port": "input",
                }}),
                # 只验形状：跑得起来就绿，工作流算错了也发现不了
                ("test_add", {"test": {
                    "id": "shape-only", "name": "只验形状",
                    "requirement": "有 greeting 字段", "inputs": {"name": "Ada"},
                    "assertions": [{"path": ["greeting"], "operator": "exists",
                                    "structural": True}],
                    "mandatory": True,
                }}),
                ("test_run", {}),
                ("draft_publish", {"explicit": True}),
                ("draft_publish", {"explicit": True}),
            ]
            name, value = operations[min(self.calls - 1, len(operations) - 1)]
            yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"c{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })

    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, StructuralOnlyProvider())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer workflow-test"}
        app_id = client.post("/api/v1/applications", headers=headers,
                             json={"name": "ShapeOnly", "requirement": "结构断言不算验收。"}).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers,
            json={"requirement": "输入姓名 name，输出 greeting 字段。",
                  "auto_publish": True, "max_turns": 8, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        assert build["status"] != "published", "纯结构验收竟然发布成功"
        events = client.get(f"/v1/streams/{build_id}", headers=headers).json()
        blocked = [
            e for e in events
            if e["type"] == "build.operation"
            and (e["data"] or {}).get("tool") == "draft_publish"
            and "没有一条断言了具体值" in str((e["data"] or {}).get("result") or "")
        ]
        assert blocked, "draft_publish 应被『证据要有效』硬门拒绝并说明原因"


def test_builder_config_keys_flattened_into_changes_are_hoisted(tmp_path: Path) -> None:
    """构建者把配置字段平铺在 changes 下时，机器替它上提。

    真机构建 7d5ffa06：4B 写 {"node_id":"end","changes":{"outputs":{...}}}，被
    Pydantic 顶回 "Extra inputs are not permitted"，原样重试 3 次，end 的输出
    始终没绑上；图结构却合法，于是带着空输出走完全程。意图毫无歧义，这一步是
    确定性的——平台早就在对 merge_config 做同样的上提。

    边界：自然语言编辑路径**不**做这件事（业主在改自己的工作流，替人猜错就是
    擅自改动），那条路径的拒绝行为另有回归测试守着。
    """

    class FlattenedUpdateProvider(ModelProvider):
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self, model: str) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

        async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            operations: list[tuple[str, dict[str, Any]]] = [
                ("draft_add_node", {"node": {
                    "id": "start", "type": "start", "title": "In",
                    "config": {"inputs": [{"name": "name", "type": "string"}]},
                }}),
                ("draft_add_node", {"node": {
                    "id": "end", "type": "end", "title": "Out", "config": {"outputs": {}},
                }}),
                ("draft_connect", {"edge": {
                    "id": "e1", "source": "start", "target": "end",
                    "source_port": "output", "target_port": "input",
                }}),
                # outputs 平铺在 changes 下——上提后应当成功
                ("draft_update_node", {"node_id": "end", "changes": {"outputs": {
                    "greeting": {"$ref": {"node_id": "start", "path": ["name"]}},
                }}}),
                ("test_add", {"test": {
                    "id": "t", "name": "问候", "requirement": "Ada → Ada",
                    "inputs": {"name": "Ada"},
                    "assertions": [{"path": ["greeting"], "operator": "equals",
                                    "expected": "Ada"}],
                    "mandatory": True,
                }}),
                ("test_run", {}),
                ("draft_publish", {"explicit": True}),
            ]
            name, value = operations[min(self.calls - 1, len(operations) - 1)]
            yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"c{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })

    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, FlattenedUpdateProvider())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer workflow-test"}
        app_id = client.post("/api/v1/applications", headers=headers,
                             json={"name": "Hoist", "requirement": "配置上提"}).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers,
            json={"requirement": "输入 name，输出 greeting。样例：Ada → Ada。",
                  "auto_publish": True, "max_turns": 10, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(900):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        assert build["status"] == "published", build.get("error")
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers).json()
        end_node = next(
            node for node in draft["snapshot"]["workflow"]["nodes"] if node["id"] == "end"
        )
        # 上提到了 config 里，而不是被拒或塞在节点顶层
        assert end_node["config"]["outputs"]["greeting"]["$ref"]["node_id"] == "start"


def test_flattened_test_add_payload_is_hoisted(tmp_path: Path) -> None:
    """test_add 的字段被平铺在顶层时替它包回去。

    真机构建 1334c391：4B 连发 15 次平铺的 test_add，平台每次只回一句
    KeyError: 'test'——既没说要包在哪，也没说它写对了什么，整单卡死在验收编写阶段。
    与 draft_update_node 的配置上提同源：意图无歧义、无人在看这一步，机器代劳。
    """

    class FlattenedTestProvider(ModelProvider):
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self, model: str) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

        async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            operations: list[tuple[str, dict[str, Any]]] = [
                ("draft_add_node", {"node": {
                    "id": "start", "type": "start", "title": "In",
                    "config": {"inputs": [{"name": "name", "type": "string"}]},
                }}),
                ("draft_add_node", {"node": {
                    "id": "end", "type": "end", "title": "Out",
                    "config": {"outputs": {"greeting": {"$ref": {
                        "node_id": "start", "path": ["name"]}}}},
                }}),
                ("draft_connect", {"edge": {
                    "id": "e1", "source": "start", "target": "end",
                    "source_port": "output", "target_port": "input",
                }}),
                # 平铺，没有 test 包装 —— 应当被上提而不是 KeyError
                ("test_add", {
                    "id": "t", "name": "问候", "requirement": "Ada → Ada",
                    "inputs": {"name": "Ada"},
                    "assertions": [{"path": ["greeting"], "operator": "equals",
                                    "expected": "Ada"}],
                    "mandatory": True,
                }),
                ("test_run", {}),
                ("draft_publish", {"explicit": True}),
            ]
            name, value = operations[min(self.calls - 1, len(operations) - 1)]
            yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"c{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })

    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, FlattenedTestProvider())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer workflow-test"}
        app_id = client.post("/api/v1/applications", headers=headers,
                             json={"name": "Flat", "requirement": "平铺测试上提"}).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers,
            json={"requirement": "输入 name，输出 greeting。样例：Ada → Ada。",
                  "auto_publish": True, "max_turns": 10, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(900):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        assert build["status"] == "published", build.get("error")
        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers).json()
        assert [t["id"] for t in draft["snapshot"]["tests"]] == ["t"]


def test_acceptance_itself_is_validated_before_it_becomes_the_criterion(
    tmp_path: Path,
) -> None:
    """验收是判据——判据本身要先被校验：输入类型对得上、断言指向真实交付字段。

    两次真机事故：
    - 13284038：测试的 inputs 把数组写成带引号的 JSON 字面量，工作流完全正确
      却红了 4 轮，修理手全在修一个没坏的工作流；
    - bdefc84a：断言写成 ["output","by_store","A店"]——多了一层 "output"，
      那是 $ref 引用节点内部产出时才需要的层级。投影里给的引用路径被搬到了
      断言里：**模型会把看到的格式用到别处**，所以这层必须机械校验。
    """
    from agent_platform.builder import WorkflowBuilder
    from agent_platform.workflow_models import (
        ApplicationSnapshot, NodeSpec, WorkflowSpec, WorkflowTestCase,
    )

    snapshot = ApplicationSnapshot(
        workflow=WorkflowSpec(
            nodes=[
                NodeSpec(id="start", type="start", title="In", config={
                    "inputs": [{"name": "sales", "type": "array"}],
                }),
                NodeSpec(id="end", type="end", title="Out", config={
                    "outputs": {"by_store": {}, "total": {}},
                }),
            ],
            edges=[],
        ),
    )

    # 输入类型：数组写成了带引号的 JSON 字面量
    bad_inputs = WorkflowTestCase(
        id="t", name="t", requirement="r",
        inputs={"sales": '[{"store":"A店","amount":1200}]'},
        assertions=[{"path": ["by_store", "A店"], "operator": "equals", "expected": 2000}],
        mandatory=True,
    )
    problems = WorkflowBuilder._test_input_type_mismatches(snapshot, bad_inputs)
    assert problems and "带引号的 JSON 字面量" in problems[0]

    # 断言路径：多了一层 output
    bad_path = WorkflowTestCase(
        id="t", name="t", requirement="r",
        inputs={"sales": [{"store": "A店", "amount": 1200}]},
        assertions=[{"path": ["output", "by_store", "A店"], "operator": "equals",
                     "expected": 2000}],
        mandatory=True,
    )
    problems = WorkflowBuilder._test_assertion_path_mismatches(snapshot, bad_path)
    assert problems, problems
    assert "可断言的字段只有" in problems[0]
    assert "by_store" in problems[0]

    # 写对的验收一律放行
    good = WorkflowTestCase(
        id="t", name="t", requirement="r",
        inputs={"sales": [{"store": "A店", "amount": 1200}]},
        assertions=[{"path": ["by_store", "A店"], "operator": "equals", "expected": 2000},
                    {"path": ["total"], "operator": "equals", "expected": 1200}],
        mandatory=True,
    )
    assert WorkflowBuilder._test_input_type_mismatches(snapshot, good) == []
    assert WorkflowBuilder._test_assertion_path_mismatches(snapshot, good) == []


def test_builder_cannot_publish_without_acceptance_evidence(tmp_path: Path) -> None:
    """验收证据硬门：构建者在测试未绿时调用 draft_publish 必须被拒。

    真实事故（2026-08-23）：某单在最后一次 test_run 明确 passed=false 之后，
    协调者以 draft_publish{explicit:true} 发布成功——平台层"只提示不阻断"的
    人类例外通道被自动化一方借用，产出的日报工作流把样例门店名硬编码进公式，
    换门店直接崩，却成了正式版 v1。业务验收可由业主知情越过，构建者不可以。
    """

    class PublishWithoutTestsProvider(ModelProvider):
        name = "scripted"

        def __init__(self) -> None:
            self.calls = 0

        def capabilities(self, model: str) -> ProviderCapabilities:
            return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

        async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
            self.calls += 1
            operations: list[tuple[str, dict[str, Any]]] = [
                ("draft_add_node", {"node": {
                    "id": "start", "type": "start", "title": "Input",
                    "config": {"inputs": [{"name": "name", "type": "string"}]},
                }}),
                ("draft_add_node", {"node": {
                    "id": "end", "type": "end", "title": "End",
                    "config": {"outputs": {"greeting": {"$ref": {
                        "node_id": "start", "path": ["name"]}}}},
                }}),
                ("draft_connect", {"edge": {
                    "id": "e1", "source": "start", "target": "end",
                    "source_port": "output", "target_port": "input",
                }}),
                # 强制测试注定失败：工作流只透传姓名，断言却要 "Hello Ada"
                ("test_add", {"test": {
                    "id": "greeting-test", "name": "问候语",
                    "requirement": "返回 Hello Ada", "inputs": {"name": "Ada"},
                    "assertions": [{"path": ["greeting"], "operator": "equals",
                                    "expected": "Hello Ada"}],
                    "mandatory": True,
                }}),
                ("test_run", {}),
                # 明知失败仍显式发布——事故当晚正是这条路径放行的
                ("draft_publish", {"explicit": True}),
                ("draft_publish", {"explicit": True}),
            ]
            name, value = operations[min(self.calls - 1, len(operations) - 1)]
            yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"c{self.calls}", "name": name, "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })

    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, PublishWithoutTestsProvider())
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer workflow-test"}
        app_id = client.post("/api/v1/applications", headers=headers,
                             json={"name": "NoEvidence", "requirement": "验收证据硬门验证。"}).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers,
            json={"requirement": "输入姓名 name，输出 greeting 字段。",
                  "auto_publish": True, "max_turns": 8, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(600):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 绝不允许在零验收证据下发布
        assert build["status"] != "published", "零验收证据竟然发布成功"
        versions = client.get(f"/api/v1/applications/{app_id}/versions", headers=headers)
        if versions.status_code == 200:
            assert not (versions.json() or []), "不应存在任何已发布版本"
        events = client.get(f"/v1/streams/{build_id}", headers=headers).json()
        blocked = [
            e for e in events
            if e["type"] == "build.operation"
            and (e["data"] or {}).get("tool") == "draft_publish"
            and "publish blocked" in str((e["data"] or {}).get("result") or "")
        ]
        assert blocked, "draft_publish 应被证据硬门拒绝并留下记录"
