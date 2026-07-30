from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_lilies_platform_api import (
    _create_assigned_application,
    _issue,
    _request,
    _settings,
)


def _iteration_node() -> dict:
    return {
        "id": "iterate",
        "type": "iteration",
        "title": "Iterate",
        "config": {
            "items": [{"value": 1}],
            "item_name": "item",
            "variables": {},
            "workflow": {
                "nodes": [
                    {
                        "id": "nested_start",
                        "type": "start",
                        "title": "Nested start",
                        "config": {
                            "inputs": [
                                {
                                    "name": "item",
                                    "label": "Item",
                                    "type": "object",
                                    "required": True,
                                }
                            ]
                        },
                    },
                    {
                        "id": "nested_target",
                        "type": "variable_assigner",
                        "title": "Nested target",
                        "config": {
                            "assignments": {
                                "value": "before",
                                "security_binding": "preserve-me",
                            }
                        },
                    },
                    {
                        "id": "nested_end",
                        "type": "end",
                        "title": "Nested end",
                        "config": {
                            "outputs": {
                                "result": {
                                    "$ref": {
                                        "node_id": "nested_target",
                                        "path": ["output"],
                                    }
                                }
                            }
                        },
                    },
                ],
                "edges": [
                    {
                        "id": "nested_start_target",
                        "source": "nested_start",
                        "target": "nested_target",
                    },
                    {
                        "id": "nested_target_end",
                        "source": "nested_target",
                        "target": "nested_end",
                    },
                ],
            },
            "output_node_id": "nested_end",
            "output_path": ["result"],
            "parallelism": 1,
        },
    }


def test_public_update_node_edits_one_nested_node_without_replacing_container(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(client, headers)["id"]
        added = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="nested-draft-add-0001",
            json={
                "expected_revision": 0,
                "op": "add_node",
                "data": {"node": _iteration_node()},
            },
        )
        assert added.status_code == 200, added.text

        updated = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="nested-draft-update-0001",
            json={
                "expected_revision": 1,
                "op": "update_node",
                "data": {
                    "node_id": "nested_target",
                    "changes": {
                        "config": {
                            "assignments": {
                                "value": "after",
                            }
                        }
                    },
                    "merge_config": True,
                },
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["revision"] == 2

        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        iteration = next(
            node
            for node in draft["snapshot"].workflow.nodes
            if node.id == "iterate"
        )
        nested_nodes = iteration.config["workflow"]["nodes"]
        target = next(
            node for node in nested_nodes if node["id"] == "nested_target"
        )
        assert target["config"]["assignments"] == {
            "value": "after",
            "security_binding": "preserve-me",
        }
        assert {node["id"] for node in nested_nodes} == {
            "nested_start",
            "nested_target",
            "nested_end",
        }

        forbidden = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="nested-draft-hidden-update-0001",
            json={
                "expected_revision": 2,
                "op": "update_node",
                "data": {
                    "node_id": "nested_target",
                    "changes": {
                        "type": "claude_agent",
                        "config": {
                            "agent_id": str(uuid4()),
                            "task": "Never run.",
                        },
                    },
                    "merge_config": False,
                },
            },
        )
        assert forbidden.status_code == 403, forbidden.text
        assert forbidden.json()["error"]["code"] == "runtime_tool_scope_denied"

