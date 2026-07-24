from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.lilies_platform_api import _redact
from agent_platform.models import AgentSpec
from agent_platform.platform_blackbox_auth import (
    BlackboxAuditEventType,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from agent_platform.tools.core import GlobInput, GrepInput
from agent_platform.workflow_models import DraftOperation, WorkflowRunState
from agent_platform.workflow_runtime import BLACKBOX_RUNTIME_TOOL_ALLOWLIST
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_lilies_platform_api import (
    ZERO_DIGEST,
    _issue,
    _request,
    _settings,
)


def _apply_operations(
    client: TestClient,
    application_id: str,
    operations: list[tuple[str, dict]],
) -> int:
    revision = 0
    for index, (operation, data) in enumerate(operations):
        result = client.portal.call(
            partial(
                client.app.state.services.applications.apply_operation,
                application_id,
                DraftOperation(
                    expected_revision=revision,
                    idempotency_key=f"runtime-isolation-draft-{index:04d}-{uuid4()}",
                    op=operation,
                    data=data,
                ),
            )
        )
        revision = int(result["revision"])
    return revision


def _linear_operations(
    middle: dict,
    *,
    tests: int = 0,
) -> list[tuple[str, dict]]:
    operations: list[tuple[str, dict]] = [
        (
            "add_node",
            {
                "node": {
                    "id": "start",
                    "type": "start",
                    "title": "Start",
                    "config": {"inputs": [{"name": "name", "type": "string"}]},
                }
            },
        ),
        ("add_node", {"node": middle}),
        (
            "add_node",
            {
                "node": {
                    "id": "end",
                    "type": "end",
                    "title": "End",
                    "config": {
                        "outputs": {
                            "value": {"$ref": {"node_id": "middle", "path": ["output"]}}
                        }
                    },
                }
            },
        ),
        (
            "add_edge",
            {
                "edge": {
                    "id": "start-middle",
                    "source": "start",
                    "target": "middle",
                    "source_port": "output",
                    "target_port": "input",
                }
            },
        ),
        (
            "add_edge",
            {
                "edge": {
                    "id": "middle-end",
                    "source": "middle",
                    "target": "end",
                    "source_port": "output",
                    "target_port": "input",
                }
            },
        ),
    ]
    for index in range(tests):
        operations.append(
            (
                "add_test",
                {
                    "test": {
                        "id": f"boundary-test-{index}",
                        "name": f"Boundary test {index}",
                        "requirement": "The workflow must remain inside its task workspace.",
                        "inputs": {"name": f"case-{index}"},
                        "assertions": [{"path": ["value"], "operator": "exists"}],
                    }
                },
            )
        )
    return operations


def _create_internal_application(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers={"Authorization": "Bearer internal-test-token"},
        json={"name": name, "requirement": "Exercise a bounded workflow runtime."},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_blackbox_test_cases_get_distinct_persisted_task_workspaces(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(client, "Isolated test cases")
        headers, _, assignment_id, session_id, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        middle = {
            "id": "middle",
            "type": "variable_assigner",
            "title": "Deterministic assignment",
            "config": {
                "assignments": {
                    "greeting": {"$ref": {"node_id": "start", "path": ["name"]}}
                }
            },
        }
        operations = _linear_operations(middle)
        operations.extend(
            [
                (
                    "add_test",
                    {
                        "test": {
                            "id": f"isolated-{index}",
                            "name": f"Isolated {index}",
                            "requirement": "Render the deterministic greeting.",
                            "inputs": {"name": name},
                            "assertions": [
                                {
                                    "path": ["value", "greeting"],
                                    "operator": "equals",
                                    "expected": name,
                                }
                            ],
                        }
                    },
                )
                for index, name in enumerate(("Ada", "Grace"))
            ]
        )
        _apply_operations(client, application_id, operations)

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="isolated-test-suite-0001",
            json={},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["passed"] is True, response.text

        runs = client.portal.call(
            partial(client.app.state.services.workflow_store.list_runs, application_id)
        )
        assert len(runs) == 2
        workspaces = {Path(run["state"].workspace_path) for run in runs}
        assert len(workspaces) == 2
        assert len({workspace.parent for workspace in workspaces}) == 1
        task_session_root = (
            client.app.state.services.settings.workspace_root.resolve()
            / ".lilies_tasks"
            / str(assignment_id)
            / str(session_id)
        )
        for run in runs:
            state = run["state"]
            workspace = Path(state.workspace_path).resolve()
            assert state.workspace_boundary == str(workspace)
            assert state.allowed_nested_application_ids == [application_id]
            assert state.allowed_runtime_tools == ["Edit", "Glob", "Grep", "Read", "Write"]
            assert state.allowed_network_hosts == []
            assert state.assignment_id == str(assignment_id)
            assert state.session_id == str(session_id)
            assert task_session_root in workspace.parents
            assert workspace.parent.name.startswith("run-")
            assert workspace.name.startswith("case-")

        evidence_run = runs[0]
        evidence_workspace = Path(evidence_run["state"].workspace_path)
        (evidence_workspace / "case-evidence.txt").write_text(
            "isolated test artifact",
            encoding="utf-8",
        )
        run_response = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{evidence_run['id']}",
            headers,
            key="isolated-test-run-artifacts-0001",
        )
        assert run_response.status_code == 200, run_response.text
        artifacts = run_response.json()["data"]["artifacts"]
        artifact_id = next(
            item["artifact_id"]
            for item in artifacts
            if item["relative_path"] == "case-evidence.txt"
        )
        artifact_response = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{evidence_run['id']}/artifacts/{artifact_id}",
            headers,
            key="isolated-test-artifact-read-0001",
        )
        assert artifact_response.status_code == 200, artifact_response.text
        assert artifact_response.json()["data"]["content"] == "isolated test artifact"


@pytest.mark.parametrize("endpoint", ["tests", "run"])
@pytest.mark.parametrize(
    ("block_type", "settings_key"),
    [
        ("tool_executor", "workspace_path"),
        ("sandbox_boundary", "workspace"),
        ("subagent_spawn", "workspace_path"),
    ],
)
def test_blackbox_rejects_declared_workspace_escape_for_tests_and_runs(
    tmp_path: Path,
    endpoint: str,
    block_type: str,
    settings_key: str,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(client, f"Escape {block_type} {endpoint}")
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        settings: dict[str, object] = {
            settings_key: str(client.app.state.services.settings.workspace_root.resolve())
        }
        if block_type == "tool_executor":
            settings.update({"tool_name": "Read", "tool_input": {"path": "evidence.txt"}})
        elif block_type == "subagent_spawn":
            settings.update({"task": "Do not execute; policy must reject first."})
        middle = {
            "id": "middle",
            "type": block_type,
            "title": "Malicious workspace override",
            "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": settings,
            },
        }
        _apply_operations(
            client,
            application_id,
            _linear_operations(middle, tests=1 if endpoint == "tests" else 0),
        )

        if endpoint == "tests":
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/tests/run",
                headers,
                key=f"workspace-escape-tests-{block_type}-0001",
                json={},
            )
        else:
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/runs",
                headers,
                key=f"workspace-escape-run-{block_type}-0001",
                json={"inputs": {"name": "Ada"}, "use_draft": True},
            )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "workspace_boundary_violation"
        assert response.json()["error"]["failure_owner"] == "user_permission"
        assert client.portal.call(
            partial(client.app.state.services.workflow_store.list_runs, application_id)
        ) == []


def test_blackbox_rejects_guessed_nested_workflow_outside_assignment(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        caller_id = _create_internal_application(client, "Assigned caller")
        target_id = _create_internal_application(client, "Unassigned target")
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(caller_id)],
        )
        middle = {
            "id": "middle",
            "type": "tool",
            "title": "Guessed nested application",
            "config": {
                "tool_name": f"workflow:{target_id}",
                "input": {"name": "Ada"},
            },
        }
        _apply_operations(client, caller_id, _linear_operations(middle))

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{caller_id}/runs",
            headers,
            key="nested-workflow-scope-denial-0001",
            json={"inputs": {"name": "Ada"}, "use_draft": True},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "nested_workflow_scope_denied"
        assert target_id not in response.text
        assert client.portal.call(
            partial(client.app.state.services.workflow_store.list_runs, caller_id)
        ) == []


@pytest.mark.parametrize("endpoint", ["tests", "run"])
@pytest.mark.parametrize(
    ("violation", "middle", "expected_code"),
    [
        (
            "bash",
            {
                "id": "middle",
                "type": "tool",
                "title": "Unscoped shell",
                "config": {"tool_name": "Bash", "input": {"command": "uname -a"}},
            },
            "runtime_tool_scope_denied",
        ),
        (
            "static-http",
            {
                "id": "middle",
                "type": "http_request",
                "title": "Unscoped HTTP",
                "config": {"method": "GET", "url": "https://example.com/private"},
            },
            "runtime_network_scope_denied",
        ),
        (
            "dynamic-http",
            {
                "id": "middle",
                "type": "http_request",
                "title": "Dynamic unscoped HTTP",
                "config": {
                    "method": "GET",
                    "url": {"$ref": {"node_id": "start", "path": ["name"]}},
                },
            },
            "runtime_network_scope_denied",
        ),
    ],
)
def test_blackbox_rejects_indirect_runtime_tool_and_network_escape(
    tmp_path: Path,
    endpoint: str,
    violation: str,
    middle: dict,
    expected_code: str,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            f"Indirect policy {endpoint} {violation}",
        )
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        _apply_operations(
            client,
            application_id,
            _linear_operations(middle, tests=1 if endpoint == "tests" else 0),
        )

        if endpoint == "tests":
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/tests/run",
                headers,
                key=f"indirect-{violation}-tests-0001",
                json={},
            )
        else:
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/runs",
                headers,
                key=f"indirect-{violation}-run-0001",
                json={"inputs": {"name": "https://attacker.invalid"}, "use_draft": True},
            )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == expected_code
        assert response.json()["error"]["failure_owner"] == "user_permission"
        assert client.portal.call(
            partial(client.app.state.services.workflow_store.list_runs, application_id)
        ) == []


def test_blackbox_runtime_uses_exact_credential_file_and_network_policy(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Credential runtime policy",
        )
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
            grant_policy={
                "file_access": False,
                "connector_access": True,
                "allowed_network_hosts": ["paperless.local"],
            },
        )
        _apply_operations(
            client,
            application_id,
            _linear_operations(
                {
                    "id": "middle",
                    "type": "http_request",
                    "title": "Allowed locked host",
                    "config": {
                        "method": "GET",
                        "url": "http://paperless.local/api/documents",
                    },
                }
            ),
        )

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="credential-runtime-policy-0001",
            json={"inputs": {"name": "Ada"}, "use_draft": True},
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["data"]["run_id"]
        run = client.portal.call(
            client.app.state.services.workflow_store.get_run,
            run_id,
        )
        assert run["state"].allowed_runtime_tools == []
        assert run["state"].allowed_network_hosts == ["paperless.local"]


def test_formal_frozen_policy_rejects_raw_http_host_bypass(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Formal raw HTTP denial",
        )
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
            grant_policy={
                "connector_access": True,
                "allowed_network_hosts": ["paperless.local"],
                "allowed_actions_digest": ZERO_DIGEST,
                "budget_digest": ZERO_DIGEST,
            },
        )
        _apply_operations(
            client,
            application_id,
            _linear_operations(
                {
                    "id": "middle",
                    "type": "http_request",
                    "title": "Forbidden raw HTTP",
                    "config": {
                        "method": "GET",
                        "url": "http://paperless.local/api/documents",
                    },
                }
            ),
        )
        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="formal-raw-http-denial-0001",
            json={"inputs": {"name": "Ada"}, "use_draft": True},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "runtime_network_scope_denied"


@pytest.mark.parametrize(
    ("grant_policy", "middle", "expected_code"),
    [
        (
            {"model_access": False},
            {
                "id": "middle",
                "type": "llm",
                "title": "Forbidden model",
                "config": {"prompt": "Do not run", "system": "Policy test"},
            },
            "runtime_model_scope_denied",
        ),
        (
            {
                "connector_access": True,
                "readable_host_objects": ["paperless.documents"],
            },
            {
                "id": "middle",
                "type": "connector_action",
                "title": "Unlisted connector operation",
                "config": {
                    "connector_id": "paperless",
                    "operation_id": "metadata.update",
                    "tenant_id": "tenant-1",
                    "actor_id": "actor-1",
                    "actor_roles": ["operator"],
                    "profile_id": "profile-1",
                    "payload": {},
                    "idempotency_key": "connector-policy-denied-0001",
                    "execution_mode": "dry_run",
                },
            },
            "runtime_connector_scope_denied",
        ),
    ],
)
def test_blackbox_runtime_rejects_disabled_model_and_unlisted_connector(
    tmp_path: Path,
    grant_policy: dict,
    middle: dict,
    expected_code: str,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            f"Rejected {expected_code}",
        )
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
            grant_policy=grant_policy,
        )
        operations = _linear_operations(middle)
        if expected_code == "runtime_model_scope_denied":
            for operation, data in operations:
                node = data.get("node")
                edge = data.get("edge")
                if (
                    operation == "add_node"
                    and isinstance(node, dict)
                    and node.get("id") == "end"
                ):
                    node["config"]["outputs"]["value"]["$ref"]["path"] = [
                        "text"
                    ]
                if (
                    operation == "add_edge"
                    and isinstance(edge, dict)
                    and edge.get("id") == "middle-end"
                ):
                    edge["source_port"] = "text"
        _apply_operations(client, application_id, operations)
        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key=f"{expected_code}-0001",
            json={"inputs": {"name": "Ada"}, "use_draft": True},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == expected_code
        assert client.portal.call(
            partial(
                client.app.state.services.workflow_store.list_runs,
                application_id,
            )
        ) == []


def test_connector_permission_and_write_limit_are_enforced_inside_the_run(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        services = client.app.state.services
        execution = MagicMock()
        execution.id = "connector-execution-0001"
        execution.operation_id = "metadata.update"
        execution.status = "succeeded"
        execution.replayed = False
        execution.response = {"updated": True}
        execution.public_receipt.return_value = {
            "execution_id": execution.id,
            "status": execution.status,
        }
        execute = AsyncMock(return_value=execution)
        services.workflow_runtime.connector_service.execute = execute

        async def terminal_run(run_id: str) -> dict:
            for _ in range(200):
                run = await services.workflow_store.get_run(run_id)
                if run["status"] in {"succeeded", "failed", "cancelled"}:
                    return run
                await asyncio.sleep(0.01)
            raise AssertionError("connector policy run did not become terminal")

        def connector_operations(
            authorization_ids: list[str],
        ) -> list[tuple[str, dict]]:
            nodes = [
                {
                    "id": f"connector-{index}",
                    "type": "connector_action",
                    "title": f"Connector write {index}",
                    "config": {
                        "connector_id": "paperless",
                        "operation_id": "metadata.update",
                        "tenant_id": "tenant-1",
                        "actor_id": "actor-1",
                        "actor_roles": ["operator"],
                        "profile_id": "profile-1",
                        "payload": {"index": index},
                        "idempotency_key": f"connector-write-{index:04d}",
                        "authorization_id": authorization_id,
                        "execution_mode": "execute",
                    },
                }
                for index, authorization_id in enumerate(
                    authorization_ids,
                    start=1,
                )
            ]
            operations: list[tuple[str, dict]] = [
                (
                    "add_node",
                    {
                        "node": {
                            "id": "start",
                            "type": "start",
                            "title": "Start",
                            "config": {"inputs": []},
                        }
                    },
                ),
                *[("add_node", {"node": node}) for node in nodes],
                (
                    "add_node",
                    {
                        "node": {
                            "id": "end",
                            "type": "end",
                            "title": "End",
                            "config": {
                                "outputs": {
                                    "value": {
                                        "$ref": {
                                            "node_id": nodes[-1]["id"],
                                            "path": ["output"],
                                        }
                                    }
                                }
                            },
                        }
                    },
                ),
            ]
            chain = ["start", *[node["id"] for node in nodes], "end"]
            for index, (source, target) in enumerate(
                zip(chain, chain[1:])
            ):
                operations.append(
                    (
                        "add_edge",
                        {
                            "edge": {
                                "id": f"connector-edge-{index}",
                                "source": source,
                                "target": target,
                                "source_port": "output",
                                "target_port": "input",
                            }
                        },
                    )
                )
            return operations

        policy = {
            "connector_access": True,
            "writable_host_operations": ["paperless.metadata.update"],
            "permission_required_actions": ["paperless.metadata.update"],
            "max_write_count": 1,
        }
        oversized_payload_app = _create_internal_application(
            client,
            "Connector payload limit",
        )
        oversized_headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(oversized_payload_app)],
            grant_policy={**policy, "max_payload_bytes": 512},
        )
        oversized_operations = connector_operations(["authorization-payload"])
        connector_node = next(
            data["node"]
            for operation, data in oversized_operations
            if operation == "add_node"
            and data.get("node", {}).get("type") == "connector_action"
        )
        connector_node["config"]["payload"] = {"value": "x" * 1_000}
        _apply_operations(
            client,
            oversized_payload_app,
            oversized_operations,
        )
        oversized = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{oversized_payload_app}/runs",
            oversized_headers,
            key="connector-payload-limit-run-0001",
            json={"inputs": {}, "use_draft": True},
        )
        assert oversized.status_code == 202, oversized.text
        oversized_run = client.portal.call(
            terminal_run,
            oversized.json()["data"]["run_id"],
        )
        assert oversized_run["status"] == "failed"
        assert "payload exceeds the assigned byte limit" in str(
            oversized_run["error"]
        )
        assert execute.await_count == 0

        missing_auth_app = _create_internal_application(
            client,
            "Missing connector authorization",
        )
        missing_headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(missing_auth_app)],
            grant_policy=policy,
        )
        _apply_operations(
            client,
            missing_auth_app,
            connector_operations([""]),
        )
        missing = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{missing_auth_app}/runs",
            missing_headers,
            key="connector-permission-run-0001",
            json={"inputs": {}, "use_draft": True},
        )
        assert missing.status_code == 202, missing.text
        missing_run = client.portal.call(
            terminal_run,
            missing.json()["data"]["run_id"],
        )
        assert missing_run["status"] == "failed"
        assert "authorization receipt" in str(missing_run["error"])
        assert execute.await_count == 0

        limited_app = _create_internal_application(
            client,
            "Connector write limit",
        )
        limited_headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(limited_app)],
            grant_policy=policy,
        )
        _apply_operations(
            client,
            limited_app,
            connector_operations(["authorization-1", "authorization-2"]),
        )
        limited = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{limited_app}/runs",
            limited_headers,
            key="connector-write-limit-run-0001",
            json={"inputs": {}, "use_draft": True},
        )
        assert limited.status_code == 202, limited.text
        limited_run = client.portal.call(
            terminal_run,
            limited.json()["data"]["run_id"],
        )
        assert limited_run["status"] == "failed"
        assert "write limit is exhausted" in str(limited_run["error"])
        assert limited_run["state"].connector_write_count == 1
        assert execute.await_count == 1


@pytest.mark.parametrize("endpoint", ["tests", "run"])
def test_blackbox_rejects_mcp_gateway_before_process_side_effect(
    tmp_path: Path,
    endpoint: str,
) -> None:
    marker = tmp_path / f"forbidden-mcp-side-effect-{endpoint}"
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(client, f"MCP escape {endpoint}")
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        middle = {
            "id": "middle",
            "type": "mcp_gateway",
            "title": "Untrusted MCP process",
            "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {
                    "servers": [
                        {
                            "name": "side-effect",
                            "command": "/usr/bin/touch",
                            "args": [str(marker)],
                        }
                    ]
                },
            },
        }
        _apply_operations(
            client,
            application_id,
            _linear_operations(middle, tests=1 if endpoint == "tests" else 0),
        )

        if endpoint == "tests":
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/tests/run",
                headers,
                key="mcp-process-denied-tests-0001",
                json={},
            )
        else:
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/runs",
                headers,
                key="mcp-process-denied-run-0001",
                json={"inputs": {"name": "Ada"}, "use_draft": True},
            )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "runtime_tool_scope_denied"
        assert marker.exists() is False
        assert client.portal.call(
            partial(client.app.state.services.workflow_store.list_runs, application_id)
        ) == []


@pytest.mark.parametrize("violation", ["dynamic_workspace", "dynamic_nested_workflow"])
def test_runtime_resolved_values_cannot_bypass_blackbox_execution_policy(
    tmp_path: Path,
    violation: str,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        caller_id = _create_internal_application(client, f"Dynamic policy {violation}")
        target_id = _create_internal_application(client, "Dynamic unassigned target")
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(caller_id)],
        )
        if violation == "dynamic_workspace":
            middle = {
                "id": "middle",
                "type": "tool_executor",
                "title": "Dynamic workspace override",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                    "settings": {
                        "tool_name": "Read",
                        "tool_input": {"path": "evidence.txt"},
                        "workspace_path": {
                            "$ref": {"node_id": "start", "path": ["name"]}
                        },
                    },
                },
            }
            inputs = {
                "name": str(client.app.state.services.settings.workspace_root.resolve())
            }
            expected_error = "task-owned execution boundary"
        else:
            middle = {
                "id": "middle",
                "type": "tool_executor",
                "title": "Dynamic nested workflow routing",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["name"]}},
                    "settings": {},
                },
            }
            inputs = {
                "name": {
                    "tool_calls": [
                        {
                            "tool_name": f"workflow:{target_id}",
                            "tool_input": {},
                        }
                    ]
                }
            }
            expected_error = "outside the assigned application scope"
        _apply_operations(client, caller_id, _linear_operations(middle))

        started = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{caller_id}/runs",
            headers,
            key=f"dynamic-policy-{violation}-0001",
            json={"inputs": inputs, "use_draft": True},
        )
        assert started.status_code == 202, started.text
        run_id = started.json()["data"]["run_id"]

        async def wait_for_terminal() -> None:
            for _ in range(100):
                task = client.app.state.services.workflow_runtime.active_tasks.get(run_id)
                if task is not None:
                    await task
                record = await client.app.state.services.workflow_store.get_run(run_id)
                if record["status"] in {"succeeded", "failed", "cancelled", "paused"}:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("workflow run did not reach a terminal status")

        client.portal.call(wait_for_terminal)
        result = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}",
            headers,
            key=f"dynamic-policy-result-{violation}-0001",
        )
        assert result.status_code == 200, result.text
        assert result.json()["data"]["status"] == "failed"
        assert expected_error in result.json()["data"]["error"]
        assert target_id not in result.text


def test_artifact_exact_once_replay_keeps_content_out_of_request_database(
    tmp_path: Path,
) -> None:
    marker = f"artifact-content-must-not-enter-db-{uuid4()}"
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(client, "Artifact request ledger")
        headers, _, assignment_id, session_id, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        run_id = str(uuid4())
        workspace = (
            client.app.state.services.settings.workspace_root
            / ".lilies_tasks"
            / str(assignment_id)
            / str(session_id)
            / "run-artifact-request-ledger"
        )
        workspace.mkdir(parents=True)
        (workspace / "evidence.txt").write_text(marker, encoding="utf-8")
        state = WorkflowRunState(
            run_id=run_id,
            application_id=application_id,
            snapshot=draft["snapshot"],
            inputs={},
            workspace_path=str(workspace),
            assignment_id=str(assignment_id),
            session_id=str(session_id),
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.update_run,
                run_id,
                status="succeeded",
                state=state,
                outputs={"result": "done"},
            )
        )
        run_response = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}",
            headers,
            key="artifact-ledger-run-read-0001",
        )
        assert run_response.status_code == 200, run_response.text
        artifact_id = run_response.json()["data"]["artifacts"][0]["artifact_id"]

        first = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
            headers,
            key="artifact-ledger-content-read-0001",
        )
        replay = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
            headers,
            key="artifact-ledger-content-read-0001",
        )
        assert first.status_code == 200, first.text
        assert replay.json() == first.json()
        assert first.json()["data"]["content"] == marker

        with sqlite3.connect(client.app.state.services.storage.db_path) as connection:
            request_row = connection.execute(
                """SELECT state,response_json,response_digest
                   FROM platform_blackbox_requests
                   WHERE operation='platform_artifact_read'"""
            ).fetchone()
            database_dump = "\n".join(connection.iterdump())
        assert request_row is not None
        assert request_row[0] == "completed"
        assert request_row[1] is None
        assert str(request_row[2]).startswith("sha256:")
        assert marker not in database_dump


def test_public_authentication_failures_are_denied_audit_events(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        services = client.app.state.services

        async def issue(expires_at: datetime):
            assignment_id = uuid4()
            session_id = uuid4()
            issued = await services.platform_blackbox_auth.issue_credential(
                TaskCredentialGrant(
                    assignment_id=assignment_id,
                    session_id=session_id,
                    scopes=[PlatformBlackboxScope.catalog_read],
                    expires_at=expires_at,
                )
            )
            return issued, assignment_id, session_id

        now = datetime.now(timezone.utc)
        expired, expired_assignment, expired_session = client.portal.call(
            issue,
            now + timedelta(hours=1),
        )
        revoked, revoked_assignment, revoked_session = client.portal.call(
            issue,
            now + timedelta(hours=1),
        )
        client.portal.call(
            partial(
                services.platform_blackbox_auth.revoke_credential,
                revoked.credential.credential_ref,
                reason="runtime isolation audit test",
            )
        )
        with sqlite3.connect(services.storage.db_path) as connection:
            connection.execute(
                "UPDATE platform_task_credentials SET expires_at=? WHERE id=?",
                (
                    (now - timedelta(seconds=1)).isoformat(),
                    str(expired.credential.credential_id),
                ),
            )

        def headers(token: str, assignment_id, session_id) -> dict[str, str]:
            return {
                "Authorization": f"Bearer {token}",
                "X-Lilies-Assignment-ID": str(assignment_id),
                "X-Lilies-Session-ID": str(session_id),
                "X-Lilies-Contract-Digest": ZERO_DIGEST,
            }

        expired_response = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers(
                expired.access_token.get_secret_value(),
                expired_assignment,
                expired_session,
            ),
            key="expired-public-audit-0001",
        )
        revoked_response = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers(
                revoked.access_token.get_secret_value(),
                revoked_assignment,
                revoked_session,
            ),
            key="revoked-public-audit-0001",
        )
        invalid_assignment = uuid4()
        invalid_session = uuid4()
        invalid_response = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers("invalid-task-token", invalid_assignment, invalid_session),
            key="invalid-public-audit-0001",
        )
        assert expired_response.status_code == 401
        assert revoked_response.status_code == 401
        assert invalid_response.status_code == 401

        audit = client.portal.call(services.platform_blackbox_auth.list_audit)
        denied = [
            event
            for event in audit
            if event.event_type is BlackboxAuditEventType.denied
            and event.idempotency_key
            in {
                "expired-public-audit-0001",
                "revoked-public-audit-0001",
                "invalid-public-audit-0001",
            }
        ]
        assert {event.reason_code for event in denied} == {
            "credential_expired",
            "credential_revoked",
            "invalid_credential",
        }
        assert len(denied) == 3


def test_redaction_preserves_token_counters_but_removes_credentials() -> None:
    payload = {
        "max_tokens": 4_096,
        "token_budget": 8_192,
        "token_count": 512,
        "input_tokens": 400,
        "output_tokens": 112,
        "token": "plain-secret",
        "access_token": "access-secret",
        "session_token": "session-secret",
        "authorization": "Bearer secret-value",
        "secret": "hidden",
        "message": "credential lpt_0123456789abcdef0123456789abcdef_"
        + "A" * 43,
        "embedded_path": "[Errno 2] missing '/Users/private/Lilies/data.db'",
        "windows_path": r"failed opening C:\\Users\\private\\secrets.txt",
        "public_url": "See https://example.com/public/manual for details",
    }
    redacted = _redact(payload)
    assert redacted["max_tokens"] == 4_096
    assert redacted["token_budget"] == 8_192
    assert redacted["token_count"] == 512
    assert redacted["input_tokens"] == 400
    assert redacted["output_tokens"] == 112
    for key in ("token", "access_token", "session_token", "authorization", "secret"):
        assert redacted[key] == "[REDACTED]"
    assert "lpt_" not in redacted["message"]
    assert "/Users/private" not in redacted["embedded_path"]
    assert r"C:\\Users\\private" not in redacted["windows_path"]
    assert "[REDACTED_PATH]" in redacted["embedded_path"]
    assert redacted["public_url"] == payload["public_url"]


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (GrepInput, {"pattern": "secret", "path": "/etc"}),
        (GrepInput, {"pattern": "secret", "path": "../private"}),
        (GrepInput, {"pattern": "secret", "path": r"C:\\private"}),
        (GrepInput, {"pattern": "secret", "glob": "../*.txt"}),
        (GlobInput, {"pattern": "../*", "path": "."}),
        (GlobInput, {"pattern": "*", "path": "/proc"}),
    ],
)
def test_blackbox_safe_file_tools_reject_non_workspace_paths(
    model: type[GrepInput] | type[GlobInput],
    payload: dict,
) -> None:
    with pytest.raises(ValueError, match="relative to the workspace"):
        model.model_validate(payload)


def test_blackbox_subagent_without_tools_is_genuinely_tool_free(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(client, "Tool-free subagent")
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        middle = {
            "id": "middle",
            "type": "subagent_spawn",
            "title": "No implicit tools",
            "config": {
                "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                "settings": {"task": "Answer without using any tool."},
            },
        }
        _apply_operations(client, application_id, _linear_operations(middle))

        fake_sandbox = MagicMock()
        fake_sandbox.workspace = client.app.state.services.settings.workspace_root
        with patch.object(
            client.app.state.services.workflow_runtime.agent_runtime.sandboxes,
            "get_or_create",
            new_callable=AsyncMock,
            return_value=fake_sandbox,
        ):
            started = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/runs",
                headers,
                key="tool-free-subagent-run-0001",
                json={"inputs": {"name": "Ada"}, "use_draft": True},
            )
            assert started.status_code == 202, started.text
            run_id = started.json()["data"]["run_id"]

            async def wait_for_terminal() -> None:
                task = client.app.state.services.workflow_runtime.active_tasks.get(run_id)
                if task is not None:
                    await task

            client.portal.call(wait_for_terminal)
        run = client.portal.call(client.app.state.services.workflow_store.get_run, run_id)
        assert run["status"] == "succeeded"
        events = client.portal.call(client.app.state.services.storage.list_events, run_id)
        subagent_started = next(event for event in events if event.type == "subagent.started")
        assert subagent_started.data["tools"] == []
        denied_tool = next(
            event
            for event in events
            if event.type == "subagent.event"
            and event.data.get("event") == "tool.failed"
        )
        assert "tool is not enabled: Write" in denied_tool.data["data"]["error"]
        assert not (Path(run["state"].workspace_path) / "done.txt").exists()
        probe = AgentSpec(
            name="Restricted empty tools",
            description="Verify actual registry definitions.",
            system_prompt="Operate without tools inside the assigned black-box run policy.",
            tools=[],
        )
        effective = client.app.state.services.workflow_runtime._restricted_agent(
            probe,
            BLACKBOX_RUNTIME_TOOL_ALLOWLIST,
        )
        assert client.app.state.services.tools.definitions_for(effective) == []
        assert client.app.state.services.tools.definitions_for(probe)


def test_blackbox_zero_test_and_publish_paths_reject_hidden_block_before_side_effects(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(client, "Hidden legacy block")
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        _apply_operations(
            client,
            application_id,
            [
                (
                    "add_node",
                    {
                        "node": {
                            "id": "legacy",
                            "type": "claude_agent",
                            "title": "Legacy hidden agent",
                            "config": {
                                "agent_id": str(uuid4()),
                                "task": "Must never execute.",
                            },
                        }
                    },
                )
            ],
        )

        tests_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="hidden-zero-tests-policy-0001",
            json={},
        )
        assert tests_response.status_code == 403, tests_response.text
        assert tests_response.json()["error"]["code"] == "runtime_tool_scope_denied"

        # Give the draft apparently current evidence so publication reaches
        # the same static graph policy instead of stopping at its evidence gate.
        revision = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )["revision"]
        client.portal.call(
            partial(
                client.app.state.services.applications.apply_operation,
                application_id,
                DraftOperation(
                    expected_revision=revision,
                    idempotency_key=f"hidden-publish-test-{uuid4()}",
                    op="add_test",
                    data={
                        "test": {
                            "id": "mandatory-hidden-policy",
                            "name": "Mandatory policy evidence",
                            "requirement": "The hidden graph must remain unpublishable.",
                            "inputs": {},
                            "assertions": [],
                            "mandatory": True,
                        }
                    },
                ),
            )
        )
        current = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        client.portal.call(
            client.app.state.services.workflow_store.mark_tested,
            application_id,
            current["revision"],
            current["content_hash"],
            {"passed": True, "validation": {"content_hash": current["content_hash"]}},
        )

        publish_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="hidden-publish-policy-0001",
            json={"acknowledge_warnings": True},
        )
        assert publish_response.status_code == 403, publish_response.text
        assert publish_response.json()["error"]["code"] == "runtime_tool_scope_denied"
        assert client.portal.call(
            client.app.state.services.workflow_store.list_runs,
            application_id,
        ) == []
        assert client.portal.call(
            client.app.state.services.workflow_store.list_versions,
            application_id,
        ) == []
        application = client.portal.call(
            client.app.state.services.workflow_store.get_application,
            application_id,
        )
        assert application["active_version"] is None
