from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.customer_runtime_projection import (
    project_public_value,
    project_runtime_events,
    project_runtime_run,
    project_runtime_snapshot,
)
from agent_platform.models import EventRecord
from tests.test_runtime import ScriptedProvider


API_TOKEN = "customer-runtime-projection-api-token"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token=API_TOKEN,
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3_600,
        lilies_local_agent_enabled=False,
        lilies_collaboration_enabled=False,
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_TOKEN}"}


def _unsafe_snapshot() -> dict[str, object]:
    return {
        "name": "Customer intake",
        "description": "Routes a customer request",
        "requirement": "Collect the request and return a reviewed answer.",
        "mode": "workflow",
        "delivery_mode": "guided",
        "agents": {
            "internal-agent": {
                "system_prompt": "Never expose this prompt",
                "workspace_path": "/private/workspace",
            }
        },
        "workflow": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "Customer request",
                    "description": "Information supplied by the customer",
                    "config": {
                        "settings": {
                            "inputs": [
                                {
                                    "name": "customer_query",
                                    "label": "Question",
                                    "type": "string",
                                    "required": True,
                                },
                                {
                                    "name": "connector_profile_id",
                                    "label": "Internal connector profile",
                                    "type": "string",
                                    "default": "production-secret-profile",
                                },
                                {
                                    "name": "connector_authorization_id",
                                    "label": "Internal authorization",
                                    "type": "string",
                                },
                                {
                                    "name": "tenant_id",
                                    "label": "Internal tenant",
                                    "type": "string",
                                },
                            ]
                        },
                        "connector": {
                            "base_url": "https://internal.invalid",
                            "api_key": "connector-secret",
                        },
                    },
                    "position": {"x": 10, "y": 20},
                },
                {
                    "id": "connector",
                    "type": "connector_request",
                    "title": "Fetch account",
                    "config": {
                        "profile_id": "production-secret-profile",
                        "authorization_id": "private-authorization",
                        "api_key": "connector-secret",
                    },
                    "position": {"x": 240, "y": 20},
                },
                {
                    "id": "developer-review",
                    "type": "developer_collaboration",
                    "title": "Private developer review",
                    "config": {"private_reason": "internal diagnosis"},
                },
            ],
            "edges": [
                {
                    "id": "start-connector",
                    "source": "start",
                    "target": "connector",
                },
                {
                    "id": "connector-developer",
                    "source": "connector",
                    "target": "developer-review",
                },
            ],
        },
        "tests": [{"name": "internal contract test", "system_prompt": "hidden"}],
    }


def _unsafe_run() -> dict[str, object]:
    return {
        "id": "run-safe-projection",
        "status": "succeeded",
        "outputs": {
            "answer": {
                "text": "The account is eligible.",
                "reasoning": "The published policy permits this business outcome.",
                "thinking": "private chain of thought",
                "raw_blocks": [{"type": "thinking", "text": "hidden"}],
                "signature": "private-model-signature",
                "private_reason": "internal diagnosis",
                "developer_response": {"patch": "private developer patch"},
                "collaboration": {"report": "private collaboration report"},
            }
        },
        "state": {
            "run_id": "run-safe-projection",
            "snapshot": _unsafe_snapshot(),
            "inputs": {
                "customer_query": "Can this customer receive service?",
                "api_key": "runtime-secret",
            },
            "workspace_path": "/private/workspace",
            "workspace_boundary": "/private",
            "allowed_runtime_tools": ["shell", "connector_admin"],
            "assignment_id": "private-assignment",
            "session_id": "private-session",
            "completed": ["start", "connector"],
            "skipped": [],
            "waiting_node_id": None,
        },
        "created_at": "2026-07-24T00:00:00Z",
        "updated_at": "2026-07-24T00:00:01Z",
    }


def _unsafe_events() -> list[dict[str, object]]:
    return [
        {
            "id": 1,
            "type": "node.completed",
            "data": {
                "node_id": "connector",
                "status": "succeeded",
                "title": "Fetch account",
                "thinking": "private even inside a public event",
                "raw_blocks": [{"text": "hidden"}],
                "signature": "private-signature",
                "private_reason": "internal diagnosis",
                "developer_response": "private response",
                "collaboration_report": "private report",
                "outputs": {"api_key": "not part of the runtime event contract"},
            },
            "created_at": "2026-07-24T00:00:01Z",
        },
        {
            "id": 2,
            "type": "node.llm.model.thinking.delta",
            "data": {"thinking": "private chain of thought"},
            "created_at": "2026-07-24T00:00:01Z",
        },
        {
            "id": 3,
            "type": "developer.response.applied",
            "data": {"status": "applied"},
            "created_at": "2026-07-24T00:00:01Z",
        },
        {
            "id": 4,
            "type": "collaboration.report.received",
            "data": {"status": "received"},
            "created_at": "2026-07-24T00:00:01Z",
        },
        {
            "id": 5,
            "type": "model.signature",
            "data": {"signature": "private-signature"},
            "created_at": "2026-07-24T00:00:01Z",
        },
        {
            "id": 6,
            "type": "runtime.private_reason.recorded",
            "data": {"private_reason": "hidden"},
            "created_at": "2026-07-24T00:00:01Z",
        },
    ]


def _assert_private_material_absent(payload: object) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).casefold()
    for marker in (
        "private chain of thought",
        "private-model-signature",
        "private-signature",
        "internal diagnosis",
        "private developer patch",
        "private response",
        "private collaboration report",
        "private report",
        "runtime-secret",
        "/private/workspace",
        "connector-secret",
        "production-secret-profile",
        "private-authorization",
        "private-assignment",
        "private-session",
        "connector_admin",
    ):
        assert marker not in encoded


def test_public_value_drops_private_runtime_fields_but_preserves_business_reasoning() -> None:
    projected = project_public_value(_unsafe_run()["outputs"])

    assert projected == {
        "answer": {
            "text": "The account is eligible.",
            "reasoning": "The published policy permits this business outcome.",
        }
    }
    _assert_private_material_absent(projected)


def test_runtime_snapshot_keeps_customer_inputs_without_connector_or_agent_config() -> None:
    projected = project_runtime_snapshot(_unsafe_snapshot())

    start = next(node for node in projected["workflow"]["nodes"] if node["id"] == "start")
    connector = next(
        node for node in projected["workflow"]["nodes"] if node["id"] == "connector"
    )
    assert start["config"]["settings"]["inputs"] == [
        {
            "label": "Question",
            "name": "customer_query",
            "required": True,
            "type": "string",
        }
    ]
    assert connector["config"] == {}
    assert projected["agents"] == {}
    assert projected["tests"] == []
    assert [node["id"] for node in projected["workflow"]["nodes"]] == [
        "start",
        "connector",
    ]
    assert projected["workflow"]["edges"] == [
        {
            "id": "start-connector",
            "source": "start",
            "target": "connector",
            "source_port": "output",
            "target_port": "input",
        }
    ]
    _assert_private_material_absent(projected)


def test_runtime_run_state_and_events_have_a_bounded_customer_contract() -> None:
    run = project_runtime_run(_unsafe_run())
    events = project_runtime_events(_unsafe_events())

    assert run["outputs"]["answer"]["reasoning"] == (
        "The published policy permits this business outcome."
    )
    assert set(run["state"]) == {
        "snapshot",
        "waiting_node_id",
        "completed",
        "skipped",
    }
    assert events == [
        {
            "id": 1,
            "type": "node.completed",
            "data": {
                "node_id": "connector",
                "status": "succeeded",
                "title": "Fetch account",
            },
            "created_at": "2026-07-24T00:00:01Z",
        }
    ]
    _assert_private_material_absent({"run": run, "events": events})


def test_customer_runtime_api_projects_application_and_run_responses(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    application = {
        "id": "application-safe-projection",
        "name": "Customer intake",
        "description": "Routes a customer request",
        "requirement": "Collect the request and return a reviewed answer.",
        "active_version": None,
        "internal_prompt": "hidden application prompt",
    }
    definition = {
        "revision": 7,
        "content_hash": "sha256:" + "1" * 64,
        "snapshot": _unsafe_snapshot(),
    }
    events = [
        EventRecord.model_validate(event | {"stream_id": "run-safe-projection"})
        for event in _unsafe_events()
    ]

    with TestClient(app) as client:
        services = client.app.state.services
        services.workflow_store.get_application = AsyncMock(return_value=application)
        services.workflow_store.get_draft = AsyncMock(return_value=definition)
        services.workflow_store.list_runs = AsyncMock(return_value=[_unsafe_run()])
        services.workflow_store.get_run = AsyncMock(return_value=_unsafe_run())
        services.storage.list_events = AsyncMock(return_value=events)

        unauthorized = client.get(
            "/api/v1/customer-runtime/applications/application-safe-projection"
        )
        application_response = client.get(
            "/api/v1/customer-runtime/applications/application-safe-projection",
            headers=_auth(),
        )
        run_response = client.get(
            "/api/v1/customer-runtime/runs/run-safe-projection",
            headers=_auth(),
        )

    assert unauthorized.status_code == 401
    assert application_response.status_code == 200, application_response.text
    assert run_response.status_code == 200, run_response.text

    application_payload = application_response.json()
    run_payload = run_response.json()
    assert application_payload["application"] == {
        "id": "application-safe-projection",
        "name": "Customer intake",
        "description": "Routes a customer request",
        "requirement": "Collect the request and return a reviewed answer.",
        "active_version": None,
    }
    assert application_payload["definition"]["source"] == "draft"
    assert application_payload["definition"]["draft_revision"] == 7
    assert application_payload["latest_run"] == run_payload["run"]
    assert application_payload["latest_events"] == run_payload["events"]
    assert run_payload["run"]["outputs"]["answer"]["reasoning"] == (
        "The published policy permits this business outcome."
    )
    assert len(run_payload["events"]) == 1
    _assert_private_material_absent(application_payload)
    _assert_private_material_absent(run_payload)
