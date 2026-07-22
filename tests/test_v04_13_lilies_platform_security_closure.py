from __future__ import annotations

from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.platform_harness import PlatformHarnessViolation
from agent_platform.workflow_models import WorkflowRunRequest, WorkflowRunState
from agent_platform.workflow_runtime import (
    MAX_NESTED_WORKFLOW_DEPTH,
    NestedWorkflowCycleDenied,
    NestedWorkflowDepthExceeded,
)
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_lilies_platform_api import (
    _create_assigned_application,
    _issue,
    _request,
    _settings,
)
from tests.test_v04_13_lilies_platform_runtime_isolation import (
    _apply_operations,
    _linear_operations,
)


def _private_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key).casefold() for key in value),
            *(
                nested
                for item in value.values()
                for nested in _private_keys(item)
            ),
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _private_keys(item)}
    return set()


def test_harness_secret_references_require_exact_owner_and_explicit_execution_scope(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        harness = client.app.state.services.harness
        client.portal.call(
            partial(
                harness.save_secret,
                owner_id="owner-a",
                name="api-token",
                value="same-owner-secret-marker",
            )
        )
        client.portal.call(
            partial(
                harness.save_secret,
                owner_id="owner-b",
                name="api-token",
                value="cross-owner-secret-marker",
            )
        )

        with pytest.raises(PlatformHarnessViolation, match="owner does not match"):
            client.portal.call(
                partial(
                    harness.inject_secret_references,
                    owner_id="owner-a",
                    payload={"content": {"$secret": "secret://owner-b/api-token"}},
                )
            )
        with pytest.raises(PlatformHarnessViolation, match="outside this execution policy"):
            client.portal.call(
                partial(
                    harness.inject_secret_references,
                    owner_id="owner-a",
                    payload={"content": {"$secret": "secret://owner-a/api-token"}},
                    allow_secret_references=False,
                )
            )


@pytest.mark.parametrize("reference_owner", ["same", "cross"])
def test_blackbox_run_rejects_secret_references_before_run_or_harness_persistence(
    tmp_path: Path,
    reference_owner: str,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(
            client,
            headers,
            key=f"secret-{reference_owner}-application-create-0001",
        )["id"]
        secret_owner = application_id if reference_owner == "same" else str(uuid4())
        client.portal.call(
            partial(
                client.app.state.services.harness.save_secret,
                owner_id=secret_owner,
                name="api-token",
                value=f"{reference_owner}-blackbox-secret-marker",
            )
        )
        middle = {
            "id": "middle",
            "type": "tool",
            "title": "Secret exfiltration attempt",
            "config": {
                "tool_name": "Write",
                "input": {
                    "path": "leaked-secret.txt",
                    "content": {
                        "$secret": f"secret://{secret_owner}/api-token",
                    },
                },
            },
        }
        _apply_operations(client, application_id, _linear_operations(middle))
        tasks_before = {
            task.id
            for task in client.portal.call(
                partial(
                    client.app.state.services.harness.list_tasks,
                    kind="workflow_run",
                )
            )
        }

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key=f"secret-{reference_owner}-run-start-0001",
            json={"inputs": {}, "use_draft": True},
        )

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "runtime_secret_scope_denied"
        assert "blackbox-secret-marker" not in response.text
        assert client.portal.call(
            partial(
                client.app.state.services.workflow_store.list_runs,
                application_id,
            )
        ) == []
        tasks_after = {
            task.id
            for task in client.portal.call(
                partial(
                    client.app.state.services.harness.list_tasks,
                    kind="workflow_run",
                )
            )
        }
        assert tasks_after == tasks_before


def test_public_run_and_test_results_recursively_remove_private_model_fields(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, assignment_id, session_id, _ = _issue(client)
        application_id = _create_assigned_application(
            client,
            headers,
            key="private-fields-application-create-0001",
        )["id"]
        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        run_id = str(uuid4())
        private_outputs = {
            "result": {
                "visible": "public-result-marker",
                "thinking": "private-thinking-marker",
                "raw_blocks": [{"text": "private-raw-block-marker"}],
                "nested": [
                    {
                        "signature": "private-signature-marker",
                        "reasoning": "private-reasoning-marker",
                        "reasoning_content": "private-reasoning-content-marker",
                        "keep": "public-nested-marker",
                    }
                ],
            }
        }
        state = WorkflowRunState(
            run_id=run_id,
            application_id=application_id,
            snapshot=draft["snapshot"],
            inputs={},
            workspace_path=".",
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
                status="running",
                state=state,
                outputs=private_outputs,
            )
        )

        run_response = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}",
            headers,
            key="private-fields-run-get-0001",
        )
        assert run_response.status_code == 200, run_response.text
        run_data = run_response.json()["data"]
        assert run_data["outputs"]["result"]["visible"] == "public-result-marker"
        assert run_data["outputs"]["result"]["nested"][0]["keep"] == "public-nested-marker"

        client.app.state.services.workflow_runtime.run_test_suite = AsyncMock(
            return_value={
                "passed": True,
                "validation": {"valid": True, "errors": [], "warnings": []},
                "summary": {"total": 1, "passed": 1, "failed": 0},
                "tests": [
                    {
                        "id": "private-field-test",
                        "passed": True,
                        "visible": "public-test-marker",
                        "thinking": "private-test-thinking-marker",
                        "raw_blocks": ["private-test-raw-block-marker"],
                        "nested": {
                            "signature": "private-test-signature-marker",
                            "reasoning": "private-test-reasoning-marker",
                        },
                    }
                ],
            }
        )
        tests_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="private-fields-tests-run-0001",
            json={},
        )
        assert tests_response.status_code == 200, tests_response.text
        tests_data = tests_response.json()["data"]
        assert tests_data["tests"][0]["visible"] == "public-test-marker"

        private_field_names = {
            "thinking",
            "raw_blocks",
            "signature",
            "reasoning",
            "reasoning_content",
        }
        assert _private_keys(run_data).isdisjoint(private_field_names)
        assert _private_keys(tests_data).isdisjoint(private_field_names)
        serialized = f"{run_response.text}\n{tests_response.text}"
        for marker in (
            "private-thinking-marker",
            "private-raw-block-marker",
            "private-signature-marker",
            "private-reasoning-marker",
            "private-reasoning-content-marker",
            "private-test-thinking-marker",
            "private-test-raw-block-marker",
            "private-test-signature-marker",
            "private-test-reasoning-marker",
        ):
            assert marker not in serialized


def test_nested_workflow_cycle_and_depth_gates_precede_run_and_harness_persistence(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(
            client,
            headers,
            key="nested-gates-application-create-0001",
        )["id"]
        request = WorkflowRunRequest(inputs={}, use_draft=True, workspace_path=".")
        tasks_before = {
            task.id
            for task in client.portal.call(
                partial(
                    client.app.state.services.harness.list_tasks,
                    kind="workflow_run",
                )
            )
        }

        with pytest.raises(NestedWorkflowCycleDenied, match="cycle"):
            client.portal.call(
                partial(
                    client.app.state.services.workflow_runtime.create_run,
                    application_id,
                    request,
                    application_call_chain=[application_id],
                )
            )
        with pytest.raises(NestedWorkflowDepthExceeded, match="depth"):
            client.portal.call(
                partial(
                    client.app.state.services.workflow_runtime.create_run,
                    application_id,
                    request,
                    application_call_chain=[
                        str(uuid4()) for _ in range(MAX_NESTED_WORKFLOW_DEPTH)
                    ],
                )
            )

        assert client.portal.call(
            partial(
                client.app.state.services.workflow_store.list_runs,
                application_id,
            )
        ) == []
        tasks_after = {
            task.id
            for task in client.portal.call(
                partial(
                    client.app.state.services.harness.list_tasks,
                    kind="workflow_run",
                )
            )
        }
        assert tasks_after == tasks_before


@pytest.mark.parametrize(
    ("method", "path_suffix", "body", "operation"),
    [
        ("GET", "", None, "platform_run_get"),
        ("POST", "/resume", {"values": {}}, "platform_run_resume"),
        ("POST", "/cancel", {}, "platform_run_cancel"),
        ("GET", "/trace", None, "platform_trace_get"),
        (
            "GET",
            f"/artifacts/{uuid4()}",
            None,
            "platform_artifact_read",
        ),
    ],
)
def test_every_public_run_route_rejects_malformed_run_id_as_invalid_request(
    tmp_path: Path,
    method: str,
    path_suffix: str,
    body: dict[str, object] | None,
    operation: str,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        _create_assigned_application(
            client,
            headers,
            key=f"malformed-{operation}-application-create-0001",
        )
        response = _request(
            client,
            method,
            f"/api/v1/lilies/runs/not-a-uuid{path_suffix}",
            headers,
            key=f"malformed-{operation}-0001",
            json=body,
        )

        assert response.status_code == 422, response.text
        payload = response.json()
        assert payload["operation"] == operation
        assert payload["error"]["code"] == "invalid_request"
        assert payload["error"]["expected"] == "UUID"
        assert payload["error"]["actual"] == "invalid"


def test_valid_but_unauthorized_run_uuid_remains_not_found(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        _create_assigned_application(
            client,
            headers,
            key="unknown-run-application-create-0001",
        )
        response = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{uuid4()}",
            headers,
            key="valid-unknown-run-get-0001",
        )

        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "not_found"


def test_blackbox_publish_rejects_context_dependent_tool_without_creating_version(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(
            client,
            headers,
            key="publish-tool-policy-application-create-0001",
        )["id"]
        middle = {
            "id": "middle",
            "type": "tool",
            "title": "Context-dependent workspace write",
            "config": {
                "tool_name": "Write",
                "input": {"path": "evidence.txt", "content": "bounded evidence"},
            },
        }
        _apply_operations(
            client,
            application_id,
            _linear_operations(middle, tests=1),
        )
        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        client.portal.call(
            client.app.state.services.workflow_store.mark_tested,
            application_id,
            draft["revision"],
            draft["content_hash"],
            {
                "passed": True,
                "validation": {"content_hash": draft["content_hash"]},
            },
        )
        assert client.portal.call(
            client.app.state.services.workflow_store.list_versions,
            application_id,
        ) == []

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-context-dependent-tool-0001",
            json={"acknowledge_warnings": False},
        )

        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "runtime_tool_scope_denied"
        assert client.portal.call(
            client.app.state.services.workflow_store.list_versions,
            application_id,
        ) == []
