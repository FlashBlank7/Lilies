from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.blocks import HTTPConfig, build_block_registry
from agent_platform.config import Settings
from agent_platform.models import Usage
from tests.test_runtime import ScriptedProvider


HEADERS = {
    "Authorization": "Bearer workflow-test",
    "Content-Type": "application/json",
}


def _app(tmp_path: Path):
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    return create_app(settings, ScriptedProvider())


def _create(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={"name": name, "requirement": "Prove configured controls reach runtime."},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    operation: str,
    data: dict[str, Any],
) -> int:
    response = client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": operation,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def _add_graph(
    client: TestClient,
    application_id: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> int:
    revision = 0
    for node in nodes:
        revision = _mutate(client, application_id, revision, "add_node", {"node": node})
    for edge in edges:
        revision = _mutate(client, application_id, revision, "add_edge", {"edge": edge})
    return revision


def _run_draft(client: TestClient, application_id: str) -> tuple[str, dict[str, Any]]:
    response = client.post(
        f"/api/v1/applications/{application_id}/runs",
        headers=HEADERS,
        json={"inputs": {}, "use_draft": True},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    record: dict[str, Any] = {}
    for _ in range(200):
        record = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
        if record["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert record.get("status") == "succeeded", record
    return run_id, record


def test_registry_exposes_editor_descriptors_for_common_block_families() -> None:
    registry = build_block_registry()
    expected_paths = {
        "llm": {"system", "prompt", "model", "temperature", "structured_output"},
        "model_turn": {"settings.system", "settings.prompt", "settings.model"},
        "http_request": {"method", "url", "headers", "timeout_seconds"},
        "tool": {"tool_name", "input"},
        "tool_executor": {"settings.tool_name", "settings.tool_input"},
        "loop": {
            "max_iterations",
            "break_condition.operator",
            "break_condition.expected",
            "break_value",
            "checkpoint_each_iteration",
        },
    }

    for block_type, paths in expected_paths.items():
        definition = registry.get(block_type)
        fields = definition.editor["fields"]
        assert paths <= {field["path"] for field in fields}
        assert all(field.get("label") and field.get("control") for field in fields)
        assert definition.config_schema.get("properties")

    loop_notices = registry.get("loop").editor["notices"]
    assert {notice["kind"] for notice in loop_notices} == {"boundary", "expert"}


def test_common_configs_round_trip_unknown_fields_and_reject_invalid_updates(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    nested = {
        "nodes": [
            {"id": "nested-start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {
                "id": "nested-end",
                "type": "end",
                "title": "End",
                "config": {"outputs": {"done": True}},
            },
        ],
        "edges": [
            {
                "id": "nested-edge",
                "source": "nested-start",
                "target": "nested-end",
                "source_port": "output",
                "target_port": "input",
            }
        ],
    }
    configs = {
        "llm": {
            "system": "Return a concise answer.",
            "prompt": "Hello",
            "model": "configured-model",
            "temperature": 0.2,
            "x_extension": {"preserve": True},
        },
        "model_turn": {
            "settings": {
                "system": "Answer precisely.",
                "prompt": "Inspect this.",
                "model": "configured-turn-model",
                "tools": ["Read"],
                "output_format": "text",
                "x_extension": "kept",
            }
        },
        "http_request": {
            "method": "POST",
            "url": "https://example.com/api",
            "headers": {"X-Mode": "guided"},
            "query": {"page": 1},
            "body": {"message": "hello"},
            "timeout_seconds": 17,
            "x_extension": "kept",
        },
        "tool": {"tool_name": "Read", "input": {"path": "README.md"}},
        "tool_executor": {
            "settings": {
                "tool_name": "Read",
                "tool_input": {"path": "README.md"},
                "workspace_path": ".",
                "x_extension": "kept",
            }
        },
        "loop": {
            "workflow": nested,
            "variables": {},
            "break_condition": {"value": False, "operator": "equals", "expected": True},
            "break_value": True,
            "max_iterations": 4,
            "output_node_id": "nested-end",
            "checkpoint_each_iteration": True,
            "x_extension": "kept",
        },
    }

    with TestClient(app) as client:
        application_id = _create(client, "Config round trip")
        revision = 0
        for index, (block_type, config) in enumerate(configs.items()):
            revision = _mutate(
                client,
                application_id,
                revision,
                "add_node",
                {
                    "node": {
                        "id": f"node-{index}",
                        "type": block_type,
                        "title": block_type,
                        "config": config,
                    }
                },
            )

        draft = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        stored = {node["type"]: node["config"] for node in draft["snapshot"]["workflow"]["nodes"]}
        assert stored == configs

        invalid_http = dict(configs["http_request"], timeout_seconds=0)
        rejected = client.post(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
            json={
                "expected_revision": revision,
                "idempotency_key": str(uuid4()),
                "op": "update_node",
                "data": {
                    "node_id": "node-2",
                    "changes": {"config": invalid_http},
                    "merge_config": False,
                },
            },
        )
        assert rejected.status_code == 422
        assert "greater than or equal to 1" in rejected.text

        invalid_turn = client.post(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
            json={
                "expected_revision": revision,
                "idempotency_key": str(uuid4()),
                "op": "update_node",
                "data": {
                    "node_id": "node-1",
                    "changes": {"config": {"settings": {"output_format": "yaml"}}},
                    "merge_config": False,
                },
            },
        )
        assert invalid_turn.status_code == 422
        assert "must be text or json" in invalid_turn.text

        unchanged = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        assert unchanged["revision"] == revision
        unchanged_configs = {
            node["type"]: node["config"] for node in unchanged["snapshot"]["workflow"]["nodes"]
        }
        assert unchanged_configs == configs


def test_saved_model_and_http_controls_change_runtime_behavior(tmp_path: Path) -> None:
    app = _app(tmp_path)
    model_calls: list[dict[str, str]] = []
    http_calls: list[dict[str, Any]] = []

    async def fake_model_text(
        run_id: str,
        model: str,
        system: str,
        prompt: str,
        node_id: str,
    ) -> tuple[str, Usage]:
        model_calls.append({"model": model, "system": system, "prompt": prompt, "node_id": node_id})
        return f"{model}|{system}|{prompt}", Usage(input_tokens=2, output_tokens=3)

    async def fake_http(
        config: HTTPConfig,
        context: dict[str, Any],
        *,
        owner_id: str,
    ) -> dict[str, Any]:
        http_calls.append(
            {
                "method": config.method,
                "url": config.url,
                "timeout_seconds": config.timeout_seconds,
                "owner_id": owner_id,
                "node_count": len(context["nodes"]),
            }
        )
        return {
            "output": {
                "method": config.method,
                "url": config.url,
                "timeout_seconds": config.timeout_seconds,
            },
            "status": 204,
            "headers": {},
        }

    app.state.services.workflow_runtime._model_text = fake_model_text
    app.state.services.workflow_runtime._http = fake_http
    nodes = [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {
            "id": "turn",
            "type": "model_turn",
            "title": "Configured turn",
            "config": {
                "settings": {
                    "system": "Configured system",
                    "prompt": "Configured prompt",
                    "model": "configured-model",
                    "output_format": "text",
                }
            },
        },
        {
            "id": "request",
            "type": "http_request",
            "title": "Configured request",
            "config": {
                "method": "PATCH",
                "url": "https://example.com/configured",
                "headers": {},
                "query": {},
                "body": None,
                "timeout_seconds": 17,
            },
        },
        {
            "id": "end",
            "type": "end",
            "title": "End",
            "config": {
                "outputs": {
                    "model_text": {"$ref": {"node_id": "turn", "path": ["text"]}},
                    "http": {"$ref": {"node_id": "request", "path": ["output"]}},
                }
            },
        },
    ]
    edges = [
        {"id": "a", "source": "start", "target": "turn", "source_port": "output", "target_port": "input"},
        {"id": "b", "source": "turn", "target": "request", "source_port": "output", "target_port": "input"},
        {"id": "c", "source": "request", "target": "end", "source_port": "output", "target_port": "input"},
    ]

    with TestClient(app) as client:
        application_id = _create(client, "Configured runtime")
        _add_graph(client, application_id, nodes, edges)
        _, run = _run_draft(client, application_id)

    assert model_calls == [
        {
            "model": "configured-model",
            "system": "Configured system",
            "prompt": "Configured prompt",
            "node_id": "turn",
        }
    ]
    assert http_calls[0]["method"] == "PATCH"
    assert http_calls[0]["timeout_seconds"] == 17
    assert run["outputs"] == {
        "model_text": "configured-model|Configured system|Configured prompt",
        "http": {
            "method": "PATCH",
            "url": "https://example.com/configured",
            "timeout_seconds": 17.0,
        },
    }


def test_loop_checkpoint_control_persists_each_runtime_iteration(tmp_path: Path) -> None:
    app = _app(tmp_path)
    nested = {
        "nodes": [
            {
                "id": "loop-start",
                "type": "start",
                "title": "Iteration",
                "config": {"inputs": [{"name": "iteration", "type": "number"}]},
            },
            {
                "id": "loop-end",
                "type": "end",
                "title": "Counter",
                "config": {
                    "outputs": {
                        "current": {
                            "$ref": {"node_id": "loop-start", "path": ["iteration"]}
                        }
                    }
                },
            },
        ],
        "edges": [
            {
                "id": "nested-edge",
                "source": "loop-start",
                "target": "loop-end",
                "source_port": "output",
                "target_port": "input",
            }
        ],
    }
    nodes = [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {
            "id": "loop",
            "type": "loop",
            "title": "Checkpointed loop",
            "config": {
                "workflow": nested,
                "variables": {},
                "break_condition": {"value": 0, "operator": "gte", "expected": 1},
                "break_value": {"$ref": {"node_id": "loop-end", "path": ["current"]}},
                "max_iterations": 4,
                "output_node_id": "loop-end",
                "checkpoint_each_iteration": True,
            },
        },
        {
            "id": "end",
            "type": "end",
            "title": "End",
            "config": {
                "outputs": {
                    "iterations": {"$ref": {"node_id": "loop", "path": ["iterations"]}},
                    "current": {"$ref": {"node_id": "loop", "path": ["output", "current"]}},
                }
            },
        },
    ]
    edges = [
        {"id": "a", "source": "start", "target": "loop", "source_port": "output", "target_port": "input"},
        {"id": "b", "source": "loop", "target": "end", "source_port": "output", "target_port": "input"},
    ]

    with TestClient(app) as client:
        application_id = _create(client, "Checkpointed loop")
        _add_graph(client, application_id, nodes, edges)
        run_id, run = _run_draft(client, application_id)
        events = client.get(f"/v1/streams/{run_id}", headers=HEADERS).json()

    with app.state.services.storage._connect() as connection:
        rows = connection.execute(
            "SELECT checkpoint_id, data_json FROM checkpoints WHERE run_id=? ORDER BY checkpoint_id",
            (run_id,),
        ).fetchall()
    checkpoints = {row["checkpoint_id"]: json.loads(row["data_json"]) for row in rows}

    assert run["outputs"] == {"iterations": 2, "current": 1}
    assert checkpoints["loop:iteration:1"]["iteration"] == 1
    assert checkpoints["loop:iteration:2"]["iteration"] == 2
    assert "loop:iteration:3" not in checkpoints
    checkpoint_events = [event for event in events if event["type"] == "loop.checkpoint.saved"]
    assert [event["data"]["checkpoint_id"] for event in checkpoint_events] == [
        "loop:iteration:1",
        "loop:iteration:2",
    ]


def test_frontend_uses_registry_fields_with_form_and_expert_round_trip() -> None:
    root = Path(__file__).resolve().parents[1]
    studio = (root / "platform/frontend/app/applications/[id]/page.tsx").read_text()
    frontend_types = (root / "platform/frontend/lib/platform.ts").read_text()

    helper = studio[studio.index("function editorFieldsForBlock"):studio.index("function configValueAtPath")]
    assert "block.editor?.fields" in helper
    assert "block.config_schema" in helper
    assert not any(block_type in helper for block_type in ("llm", "model_turn", "http_request", "loop"))
    assert "configFromEditorValues(configEditorBase, fields, configFieldValues)" in studio
    assert "setConfigEditorBase(config)" in studio
    assert "parseConfigObject(configText)" in studio
    assert 'data-config-editor="schema-form"' in studio
    assert 'data-config-editor="expert-json"' in studio
    assert 'data-config-editor-mode="form"' in studio
    assert 'data-config-editor-mode="json"' in studio
    assert "export type BlockEditorField" in frontend_types
    assert "fields?: BlockEditorField[]" in frontend_types
    assert "notices?: BlockEditorNotice[]" in frontend_types
