from __future__ import annotations

import json
import time
from pathlib import Path
from typing import AsyncIterator

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
